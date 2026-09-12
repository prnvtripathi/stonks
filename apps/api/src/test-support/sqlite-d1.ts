import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type { D1Database, D1Result, D1Statement } from "../repositories";

/**
 * A local SQLite-backed stand-in for Cloudflare D1, built from the real
 * forward migrations. It exists so repository tests exercise the actual SQL
 * this code sends -- window functions, JSON1, `INSERT ... SELECT` -- rather
 * than a hand-rolled fake that can drift from what D1 really executes, and
 * so a test can assert on the *number* of statements/rows a repository
 * method uses without duplicating its SQL.
 */
const MIGRATION_FILES = [
  "0001_market_schema.sql",
  "0003_global_saved_screens.sql",
  "0004_corporate_actions.sql",
  "0005_instrument_snapshots.sql",
  "0006_bounded_instrument_snapshots.sql",
  "0007_immutable_screen_run_snapshots.sql",
  "0008_bounded_run_result_pagination.sql",
];

export interface SqliteD1Probe {
  /** Number of distinct `db.prepare(...)` calls the repository made. */
  statementCount: number;
  /** Total rows returned across every `.all()` call (never `.first()`, which is always one scalar/row). */
  materializedRows: number;
  reset(): void;
}

export interface SqliteD1Harness {
  readonly db: D1Database;
  readonly sqlite: DatabaseSync;
  readonly probe: SqliteD1Probe;
}

export function migrationsDir(): string {
  return resolve(import.meta.dirname, "../../../../db/migrations");
}

export function createSqliteD1(): SqliteD1Harness {
  const sqlite = new DatabaseSync(":memory:");
  const dir = migrationsDir();
  for (const file of MIGRATION_FILES) sqlite.exec(readFileSync(resolve(dir, file), "utf8"));

  const probe: SqliteD1Probe = {
    statementCount: 0,
    materializedRows: 0,
    reset() { this.statementCount = 0; this.materializedRows = 0; },
  };

  const db: D1Database = {
    prepare(sql: string): D1Statement {
      probe.statementCount += 1;
      let bound: readonly unknown[] = [];
      const statement: D1Statement = {
        bind(...values: unknown[]): D1Statement { bound = values; return statement; },
        async first<T extends Record<string, unknown>>(): Promise<T | null> {
          const row = sqlite.prepare(sql).get(...(bound as never[])) as T | undefined;
          return row ?? null;
        },
        async all<T extends Record<string, unknown>>(): Promise<{ results: readonly T[] }> {
          const rows = sqlite.prepare(sql).all(...(bound as never[])) as T[];
          probe.materializedRows += rows.length;
          return { results: rows };
        },
        async run(): Promise<D1Result> {
          sqlite.prepare(sql).run(...(bound as never[]));
          return { success: true };
        },
      };
      return statement;
    },
    async batch(statements: readonly D1Statement[]): Promise<readonly D1Result[]> {
      sqlite.exec("BEGIN");
      try {
        const results: D1Result[] = [];
        for (const statement of statements) results.push(await statement.run());
        sqlite.exec("COMMIT");
        return results;
      } catch (cause) {
        sqlite.exec("ROLLBACK");
        throw cause;
      }
    },
  };

  return { db, sqlite, probe };
}

export function seedDataset(sqlite: DatabaseSync, datasetId: string, effectiveDate = "2026-09-04"): void {
  sqlite.prepare("INSERT INTO datasets (dataset_id, status, created_at, effective_date) VALUES (?, 'active', ?, ?)").run(datasetId, effectiveDate, effectiveDate);
  sqlite.prepare("INSERT INTO active_dataset (singleton, dataset_id, changed_at) VALUES (1, ?, ?)").run(datasetId, effectiveDate);
}

export function seedScreen(sqlite: DatabaseSync, screenId: string, name: string, expression: string, timestamp = "2026-09-01T00:00:00.000Z"): void {
  sqlite.prepare("INSERT INTO saved_screens (screen_id, name, expression, created_at, updated_at) VALUES (?, ?, ?, ?, ?)").run(screenId, name, expression, timestamp, timestamp);
}
