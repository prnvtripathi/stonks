import { describe, expect, it, vi } from "vitest";
import { createApi, createTestAccessVerifier, D1ResearchStore, MemoryResearchStore, verifyAccessRequest, type ApiEnv, type D1Database, type D1Result, type D1Statement } from "./index";

const env: ApiEnv = {
  accessTeamDomain: "https://access.example.com",
  accessAudience: "audience",
  allowedEmails: ["owner@example.com"],
  allowedOrigin: "https://dashboard.example.com",
  maxBodyBytes: 16_384,
  activeDatasetId: "dataset-1",
};

async function token(email = "owner@example.com", overrides: Partial<{ exp: number; aud: string }> = {}) {
  return await createApi({ env, store: new MemoryResearchStore(), accessVerifier: createTestAccessVerifier("test-secret"), testAccessSecret: "test-secret" }).issueTestToken({ email, ...overrides });
}

const testApi = () => createApi({ env, store: new MemoryResearchStore(), accessVerifier: createTestAccessVerifier("test-secret"), testAccessSecret: "test-secret" });

function fakeRows<T extends Record<string, unknown>>(rows: readonly Record<string, unknown>[]): readonly T[] {
  return rows as unknown as readonly T[];
}

function request(path: string, init: RequestInit = {}): Request {
  return new Request(`https://dashboard.example.com${path}`, init);
}

describe("private research API", () => {
  it("rejects missing Access identity", async () => {
    const api = testApi();
    expect((await api.fetch(request("/api/v1/status"))).status).toBe(401);
  });

  it("rejects invalid issuer, audience, signature, expiry, and email", async () => {
    const api = testApi();
    for (const bearer of [
      await token("owner@example.com", { aud: "wrong" }),
      await token("other@example.com"),
      "eyJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJodHRwczovL2FjY2Vzcy5leGFtcGxlLmNvbSIsImF1ZCI6ImF1ZGllbmNlIiwiZW1haWwiOiJvd25lckBleGFtcGxlLmNvbSIsImV4cCI6OTk5OTk5OTk5OX0.bad-signature",
    ]) {
      expect((await api.fetch(request("/api/v1/status", { headers: { Authorization: `Bearer ${bearer}` } }))).status).toBe(401);
    }
    const expired = await token("owner@example.com", { exp: Math.floor(Date.now() / 1000) - 1 });
    expect((await api.fetch(request("/api/v1/status", { headers: { Authorization: `Bearer ${expired}` } }))).status).toBe(401);
  });

  it("enforces origin, methods, content type, body size, and schema guards", async () => {
    const api = testApi();
    const auth = { Authorization: `Bearer ${await token()}` };
    expect((await api.fetch(request("/api/v1/status", { method: "OPTIONS" }))).headers.get("Allow")).toBe("GET, OPTIONS");
    expect((await api.fetch(request("/api/v1/screens", { method: "OPTIONS" }))).headers.get("Allow")).toBe("GET, POST, OPTIONS");
    expect((await api.fetch(request("/api/v1/screens/screen-1", { method: "OPTIONS" }))).headers.get("Allow")).toBe("GET, PUT, OPTIONS");
    expect((await api.fetch(request("/api/v1/status", { headers: { ...auth, Origin: "https://evil.example" } }))).status).toBe(403);
    const statusMethod = await api.fetch(request("/api/v1/status", { method: "POST", headers: auth }));
    expect(statusMethod.status).toBe(405);
    expect(statusMethod.headers.get("Allow")).toBe("GET, OPTIONS");
    const metricsMethod = await api.fetch(request("/api/v1/metrics", { method: "POST", headers: auth }));
    expect(metricsMethod.status).toBe(405);
    expect(metricsMethod.headers.get("Allow")).toBe("GET, OPTIONS");
    expect((await api.fetch(request("/api/v1/unknown", { method: "DELETE", headers: auth }))).status).toBe(404);
    expect((await api.fetch(request("/api/v1/screens", { method: "POST", headers: { ...auth, Origin: env.allowedOrigin }, body: JSON.stringify({ name: "x", source: "x" }) }))).status).toBe(415);
    expect((await api.fetch(request("/api/v1/screens", { method: "POST", headers: { ...auth, "Content-Type": "application/json", Origin: env.allowedOrigin }, body: "{}" }))).status).toBe(400);
  });

  it("saves a valid screen and returns it", async () => {
    const api = testApi();
    const bearer = await token();
    const response = await api.fetch(request("/api/v1/screens", {
      method: "POST",
      headers: { Authorization: `Bearer ${bearer}`, "Content-Type": "application/json", Origin: env.allowedOrigin },
      body: JSON.stringify({ name: "Momentum", source: "Volume > 500" }),
    }));
    expect(response.status).toBe(201);
    const screen = await response.json() as { name: string; source: string; languageVersion: string };
    expect(screen).toMatchObject({ name: "Momentum", source: "Volume > 500", languageVersion: "v1" });
  });

  it("updates a saved screen in place and preserves its identity and history", async () => {
    const api = testApi();
    const bearer = await token();
    const headers = { Authorization: `Bearer ${bearer}`, "Content-Type": "application/json", Origin: env.allowedOrigin };
    const created = await api.fetch(request("/api/v1/screens", { method: "POST", headers, body: JSON.stringify({ name: "Momentum", source: "Volume > 500" }) }));
    const original = await created.json() as { id: string; createdAt: string; updatedAt: string };
    const updated = await api.fetch(request(`/api/v1/screens/${original.id}`, { method: "PUT", headers: { ...headers, Origin: env.allowedOrigin }, body: JSON.stringify({ name: "Momentum revised", source: "Volume > 1000" }) }));
    expect(updated.status).toBe(200);
    const revised = await updated.json() as { id: string; name: string; source: string; createdAt: string; updatedAt: string };
    expect(revised).toMatchObject({ id: original.id, name: "Momentum revised", source: "Volume > 1000", createdAt: original.createdAt });
    expect(revised.updatedAt).not.toBe(original.updatedAt);
  });

  it("keeps saved screens and run history visible when the active dataset changes", async () => {
    const store = new MemoryResearchStore("dataset-old");
    const api = createApi({ env, store, accessVerifier: createTestAccessVerifier("test-secret"), testAccessSecret: "test-secret" });
    const bearer = await api.issueTestToken({ email: "owner@example.com" });
    const headers = { Authorization: `Bearer ${bearer}`, "Content-Type": "application/json", Origin: env.allowedOrigin };
    const saved = await api.fetch(request("/api/v1/screens", { method: "POST", headers, body: JSON.stringify({ name: "Momentum", source: "Volume > 500" }) }));
    const screen = await saved.json() as { id: string };
    expect((await api.fetch(request(`/api/v1/screens/${screen.id}/runs`, { method: "POST", headers }))).status).toBe(201);
    store.setActiveDataset("dataset-new");
    expect((await api.fetch(request(`/api/v1/screens/${screen.id}`, { headers }))).status).toBe(200);
    expect(((await (await api.fetch(request("/api/v1/screens", { headers }))).json()) as { data: unknown[] }).data).toHaveLength(1);
    const history = await api.fetch(request(`/api/v1/screens/${screen.id}/runs`, { headers }));
    expect(((await history.json()) as { runs: { datasetId: string }[] }).runs).toMatchObject([{ datasetId: "dataset-old" }]);
    expect((await api.fetch(request(`/api/v1/screens/${screen.id}/runs`, { method: "POST", headers }))).status).toBe(201);
    const crossDatasetHistory = (await (await api.fetch(request(`/api/v1/screens/${screen.id}/runs`, { headers }))).json()) as { runs: { datasetId: string; matches: { entered: boolean; exited: boolean }[] }[] };
    expect(crossDatasetHistory.runs).toHaveLength(2);
  });

  it("uses the immediately prior successful run across dataset promotions for entries and exits", async () => {
    const store = new MemoryResearchStore("dataset-old");
    const instrument = (instrumentId: string, active: boolean) => store.instruments.set(instrumentId, {
      instrumentId,
      symbol: instrumentId,
      name: instrumentId,
      assetClass: "equity",
      active,
      metrics: { volume: 10 },
    });
    instrument("one", true);
    instrument("two", false);
    const screen = await store.createScreen({ name: "Volume", source: "Volume > 1", languageVersion: "v1", createdAt: "2026-01-01", updatedAt: "2026-01-01" });
    const first = await store.runScreen("dataset-old", screen, "2026-01-01");

    store.setActiveDataset("dataset-new");
    instrument("one", false);
    instrument("two", true);
    const second = await store.runScreen("dataset-new", screen, "2026-02-01");

    instrument("one", true);
    instrument("two", false);
    const third = await store.runScreen("dataset-new", screen, "2026-03-01");
    const flags = (run: typeof first) => Object.fromEntries(run.matches.map((match) => [match.instrumentId, { entered: match.entered, exited: match.exited }]));

    expect(flags(first)).toEqual({ one: { entered: true, exited: false } });
    expect(flags(second)).toEqual({ two: { entered: true, exited: false }, one: { entered: false, exited: true } });
    expect(flags(third)).toEqual({ one: { entered: true, exited: false }, two: { entered: false, exited: true } });
    expect(second.matches.find((match) => match.instrumentId === "one")?.rank).toBe(0);
    expect(third.matches.find((match) => match.instrumentId === "two")?.rank).toBe(0);
    expect((await store.listRuns(screen.id)).map((run) => run.effectiveDate)).toEqual(["2026-03-01", "2026-02-01", "2026-01-01"]);
  });

  it("does not repeat an exit after an instrument remains absent", async () => {
    const store = new MemoryResearchStore("dataset-1");
    store.instruments.set("one", { instrumentId: "one", symbol: "ONE", name: "One", assetClass: "equity", active: true, metrics: { volume: 10 } });
    const screen = await store.createScreen({ name: "Volume", source: "Volume > 1", languageVersion: "v1", createdAt: "2026-01-01", updatedAt: "2026-01-01" });
    await store.runScreen("dataset-1", screen, "2026-01-01");
    store.instruments.set("one", { instrumentId: "one", symbol: "ONE", name: "One", assetClass: "equity", active: false, metrics: { volume: 10 } });
    const second = await store.runScreen("dataset-1", screen, "2026-01-02");
    const third = await store.runScreen("dataset-1", screen, "2026-01-03");

    expect(second.matches.map((match) => ({ instrumentId: match.instrumentId, exited: match.exited, rank: match.rank }))).toEqual([{ instrumentId: "one", exited: true, rank: 0 }]);
    expect(third.matches).toEqual([]);
  });

  it("rejects missing mutation origin, unknown fields, invalid pagination, and oversized bytes", async () => {
    const api = testApi();
    const bearer = await api.issueTestToken({ email: "owner@example.com" });
    const auth = { Authorization: `Bearer ${bearer}`, "Content-Type": "application/json" };
    expect((await api.fetch(request("/api/v1/screens", { method: "POST", headers: auth, body: JSON.stringify({ name: "x", source: "Volume > 1" }) }))).status).toBe(403);
    expect((await api.fetch(request("/api/v1/screens", { method: "POST", headers: { ...auth, Origin: env.allowedOrigin }, body: JSON.stringify({ name: "x", source: "Volume > 1", extra: true }) }))).status).toBe(400);
    expect((await api.fetch(request("/api/v1/screens?limit=nope", { headers: { ...auth, Origin: env.allowedOrigin } }))).status).toBe(400);
    const tinyEnv = { ...env, maxBodyBytes: 10 };
    const tinyApi = createApi({ env: tinyEnv, store: new MemoryResearchStore(), accessVerifier: createTestAccessVerifier("test-secret"), testAccessSecret: "test-secret" });
    const tinyToken = await tinyApi.issueTestToken({ email: "owner@example.com" });
    expect((await tinyApi.fetch(request("/api/v1/screens", { method: "POST", headers: { Authorization: `Bearer ${tinyToken}`, "Content-Type": "application/json", Origin: env.allowedOrigin }, body: JSON.stringify({ name: "long", source: "Volume > 1" }) }))).status).toBe(413);
  });

  it("serves private chart bodies without exposing an object URL", async () => {
    const store = new MemoryResearchStore();
    store.charts.set("INFY", { body: "{\"points\":[]}", contentType: "application/json" });
    const api = createApi({ env, store, accessVerifier: createTestAccessVerifier("test-secret"), testAccessSecret: "test-secret" });
    const bearer = await api.issueTestToken({ email: "owner@example.com" });
    const headers = { Authorization: `Bearer ${bearer}`, Origin: env.allowedOrigin };
    const response = await api.fetch(request("/api/v1/instruments/INFY/chart", { headers }));
    expect(response.status).toBe(200);
    expect(response.headers.get("Cache-Control")).toContain("private");
    expect(await response.text()).not.toContain("http");
    expect((await api.fetch(request("/api/v1/instruments/MISSING/chart", { headers }))).status).toBe(404);
  });

  it("returns active-dataset results with clause and momentum explanations", async () => {
    const store = new MemoryResearchStore("dataset-1");
    store.instruments.set("INFY", { instrumentId: "INFY", symbol: "INFY", name: "Infosys", assetClass: "equity", active: true, metrics: { volume: 10, momentum_score: 82, rs_rating: 82, return_6m: 70, return_3m: 60, trend_strength: 90, high_52w_proximity: 80, volume_confirmation: 75 }, metricRows: [{ metric: "momentum_score", value: 82, state: "present", effectiveDate: "2026-09-04" }] });
    const api = createApi({ env, store, accessVerifier: createTestAccessVerifier("test-secret"), testAccessSecret: "test-secret" });
    const bearer = await api.issueTestToken({ email: "owner@example.com" });
    const headers = { Authorization: `Bearer ${bearer}`, "Content-Type": "application/json", Origin: env.allowedOrigin };
    const saved = await api.fetch(request("/api/v1/screens", { method: "POST", headers, body: JSON.stringify({ name: "Volume", source: "Volume > 1" }) }));
    const screen = await saved.json() as { id: string };
    await api.fetch(request(`/api/v1/screens/${screen.id}/runs`, { method: "POST", headers }));
    const result = await api.fetch(request(`/api/v1/screens/${screen.id}/results?limit=1&sort=score`, { headers }));
    expect(result.status).toBe(200);
    const body = await result.json() as { run: { matches: { symbol: string; explanation: { clauses: unknown[]; momentum: { coverage: number } } }[] }; pagination: { total: number } };
    expect(body.pagination.total).toBe(1);
    expect(body.run.matches[0]).toMatchObject({ symbol: "INFY", explanation: { clauses: expect.any(Array), momentum: { coverage: 1 } } });
  });

  it("explains only saved AST predicates with boolean context and tri-state values", async () => {
    const store = new MemoryResearchStore("dataset-1");
    store.instruments.set("ONE", { instrumentId: "ONE", symbol: "ONE", name: "One", assetClass: "equity", active: true, metricRows: [
      { metric: "volume", value: 10, state: "present" },
      { metric: "return_3m", value: null, state: "missing" },
      { metric: "roe", value: 0.2, state: "present" },
      { metric: "momentum_score", value: 0.8, state: "present" },
    ] });
    const screen = await store.createScreen({ name: "Compound", source: "Volume > 5 OR Return over 3months > 10%", languageVersion: "v1", createdAt: "2026-01-01", updatedAt: "2026-01-01" });
    const run = await store.runScreen("dataset-1", screen, "2026-01-01");
    const clauses = run.matches[0]?.explanation.clauses ?? [];
    expect(clauses).toHaveLength(2);
    expect(clauses.map((clause) => clause.metric)).toEqual(["volume", "return_3m"]);
    expect(clauses.map((clause) => clause.result)).toEqual(["Matched", "Unavailable"]);
    expect(clauses[1]?.clause).toContain("OR");
    expect(clauses.map((clause) => clause.metric)).not.toContain("roe");
  });

  it("orders same-day runs by completion time before the stable ID", async () => {
    const store = new MemoryResearchStore("dataset-1");
    store.runs.set("dataset-1:screen-1", [
      { id: "z-uuid", screenId: "screen-1", datasetId: "dataset-1", effectiveDate: "2026-09-07", completedAt: "2026-09-07T10:00:00.000Z", matchCount: 0, status: "complete", matches: [] },
      { id: "a-uuid", screenId: "screen-1", datasetId: "dataset-1", effectiveDate: "2026-09-07", completedAt: "2026-09-07T11:00:00.000Z", matchCount: 0, status: "complete", matches: [] },
    ]);
    expect((await store.listRuns("screen-1")).map((run) => run.id)).toEqual(["a-uuid", "z-uuid"]);
  });

  it("surfaces a D1 mutation failure instead of reporting a saved screen", async () => {
    class FailingStatement implements D1Statement {
      bind(..._values: unknown[]): D1Statement { return this; }
      async first<T extends Record<string, unknown> = Record<string, unknown>>(): Promise<T | null> { return null; }
      async all<T extends Record<string, unknown> = Record<string, unknown>>(): Promise<{ results: readonly T[] }> { return { results: [] }; }
      async run(): Promise<{ success: false }> { return { success: false }; }
    }
    const failingDb: D1Database = { prepare: () => new FailingStatement() };
    await expect(new D1ResearchStore(failingDb).createScreen({ name: "x", source: "Volume > 1", languageVersion: "v1", createdAt: "2026-09-07T00:00:00.000Z", updatedAt: "2026-09-07T00:00:00.000Z" })).rejects.toThrow("D1 mutation failed");
  });

  it("maps persisted exit ordinals to API rank zero", async () => {
    class RunHistoryStatement implements D1Statement {
      public constructor(private readonly sql: string) {}
      bind(..._values: unknown[]): D1Statement { return this; }
      async first<T extends Record<string, unknown> = Record<string, unknown>>(): Promise<T | null> { return null; }
      async all<T extends Record<string, unknown> = Record<string, unknown>>(): Promise<{ results: readonly T[] }> {
        if (this.sql.includes("FROM screen_runs")) return { results: [{ run_id: "run-1", screen_id: "screen-1", dataset_id: "dataset-1", effective_date: "2026-09-07", result_count: 1, status: "complete" }] as unknown as readonly T[] };
        if (this.sql.includes("FROM screen_matches")) return { results: [{ instrument_id: "one", ordinal: 1, score: 2, explanation_json: JSON.stringify({ matched: true, text: "matched", metrics: ["volume"] }), entered: 1, exited: 0 }, { instrument_id: "two", ordinal: 2, score: 1, explanation_json: JSON.stringify({ matched: false, text: "exited", metrics: ["volume"] }), entered: 0, exited: 1 }] as unknown as readonly T[] };
        return { results: [] };
      }
      async run(): Promise<D1Result> { return { success: true }; }
    }
    const db: D1Database = { prepare: (sql) => new RunHistoryStatement(sql) };
    const [run] = await new D1ResearchStore(db).listRuns("screen-1");
    expect(run?.matches.map((match) => ({ instrumentId: match.instrumentId, rank: match.rank, exited: match.exited }))).toEqual([
      { instrumentId: "one", rank: 1, exited: false },
      { instrumentId: "two", rank: 0, exited: true },
    ]);
  });

  it("does not repeat a persisted exit after an instrument remains absent", async () => {
    const state: {
      present: boolean;
      runs: { runId: string; screenId: string; datasetId: string; effectiveDate: string; resultCount: number; status: string }[];
      matches: { datasetId: string; runId: string; ordinal: number; instrumentId: string; score: number | null; explanation: string; entered: number; exited: number }[];
    } = { present: true, runs: [], matches: [] };
    class HistoryStatement implements D1Statement {
      private values: unknown[] = [];
      public constructor(private readonly sql: string) {}
      bind(...values: unknown[]): D1Statement { this.values = values; return this; }
      async first<T extends Record<string, unknown> = Record<string, unknown>>(): Promise<T | null> { return null; }
      async all<T extends Record<string, unknown> = Record<string, unknown>>(): Promise<{ results: readonly T[] }> {
        if (this.sql.includes("FROM instruments AS i")) return { results: (state.present ? [{ instrument_id: "one", score: 10 }] : []) as unknown as readonly T[] };
        if (this.sql.includes("FROM screen_runs")) return { results: [...state.runs].sort((left, right) => right.effectiveDate.localeCompare(left.effectiveDate) || right.runId.localeCompare(left.runId)).map((run) => ({ run_id: run.runId, screen_id: run.screenId, dataset_id: run.datasetId, effective_date: run.effectiveDate, result_count: run.resultCount, status: run.status })) as unknown as readonly T[] };
        if (this.sql.includes("FROM screen_matches")) return { results: state.matches.filter((match) => match.runId === String(this.values[1])).map((match) => ({ instrument_id: match.instrumentId, ordinal: match.ordinal, score: match.score, explanation_json: match.explanation, entered: match.entered, exited: match.exited })) as unknown as readonly T[] };
        return { results: [] };
      }
      async run(): Promise<D1Result> {
        if (this.sql.includes("INSERT INTO screen_matches")) state.matches.push({ datasetId: String(this.values[0]), runId: String(this.values[1]), ordinal: Number(this.values[2]), instrumentId: String(this.values[3]), score: this.values[4] == null ? null : Number(this.values[4]), explanation: String(this.values[5]), entered: Number(this.values[6]), exited: Number(this.values[7]) });
        if (this.sql.includes("INSERT INTO screen_runs")) state.runs.push({ datasetId: String(this.values[0]), runId: String(this.values[1]), screenId: String(this.values[2]), effectiveDate: String(this.values[3]), resultCount: Number(this.values[4]), status: "complete" });
        return { success: true };
      }
    }
    const db: D1Database = { prepare: (sql) => new HistoryStatement(sql) };
    const store = new D1ResearchStore(db);
    const screen = { id: "screen-1", name: "x", source: "Volume > 1", languageVersion: "v1", createdAt: "2026-01-01", updatedAt: "2026-01-01" } as const;
    await store.runScreen("dataset-1", screen, "2026-01-01");
    state.present = false;
    const second = await store.runScreen("dataset-1", screen, "2026-01-02");
    const third = await store.runScreen("dataset-1", screen, "2026-01-03");

    expect(second.matches.map((match) => ({ instrumentId: match.instrumentId, exited: match.exited, rank: match.rank }))).toEqual([{ instrumentId: "one", exited: true, rank: 0 }]);
    expect(third.matches).toEqual([]);
  });

  it("rejects a match-write failure without leaving a complete run", async () => {
    const statements: string[] = [];
    class FailingMatchStatement implements D1Statement {
      private sql = "";
      bind(..._values: unknown[]): D1Statement { return this; }
      async first<T extends Record<string, unknown> = Record<string, unknown>>(): Promise<T | null> { return null; }
      async all<T extends Record<string, unknown> = Record<string, unknown>>(): Promise<{ results: readonly T[] }> {
        if (this.sql.includes("SELECT screen_id")) return { results: fakeRows<T>([{ screen_id: "screen-1", name: "x", expression: "Volume > 1", created_at: "2026-01-01", updated_at: "2026-01-01" }]) };
        if (this.sql.includes("SELECT run_id")) return { results: [] };
        if (this.sql.includes("SELECT i.instrument_id")) return { results: fakeRows<T>([{ instrument_id: "one", score: 1 }, { instrument_id: "two", score: 2 }]) };
        return { results: [] };
      }
      async run(): Promise<D1Result> { statements.push(this.sql); return { success: this.sql.includes("screen_matches") ? false : true }; }
      public setSql(sql: string): void { this.sql = sql; }
    }
    const db: D1Database = { prepare: (sql) => { const statement = new FailingMatchStatement(); statement.setSql(sql); return statement; } };
    const store = new D1ResearchStore(db);
    const screen = { id: "screen-1", name: "x", source: "Volume > 1", languageVersion: "v1", createdAt: "2026-01-01", updatedAt: "2026-01-01" } as const;
    await expect(store.runScreen("dataset-1", screen, "2026-09-07")).rejects.toThrow("D1 mutation failed");
    expect(statements.some((sql) => sql.includes("screen_runs"))).toBe(false);
  });

  it("verifies a production-style RS256 Access assertion and refreshes an unknown kid", async () => {
    const rsaKeyParams = { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" } as const;
    const keyPair = await crypto.subtle.generateKey(rsaKeyParams, true, ["sign", "verify"]);
    const publicJwk = await crypto.subtle.exportKey("jwk", keyPair.publicKey);
    const encode = (value: unknown) => btoa(String.fromCharCode(...new TextEncoder().encode(JSON.stringify(value)))).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
    const secondKeyPair = await crypto.subtle.generateKey(rsaKeyParams, true, ["sign", "verify"]);
    const secondPublicJwk = await crypto.subtle.exportKey("jwk", secondKeyPair.publicKey);
    const sign = async (kid: string, options: { readonly email?: string; readonly exp?: number; readonly iss?: string; readonly aud?: string; readonly signingKey?: typeof keyPair.privateKey } = {}) => {
      const header = encode({ alg: "RS256", typ: "JWT", kid });
      const claims = encode({ iss: options.iss ?? "https://access.example.com", aud: options.aud ?? "audience", email: options.email ?? "owner@example.com", exp: options.exp ?? Math.floor(Date.now() / 1000) + 300 });
      const signature = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", options.signingKey ?? keyPair.privateKey, new TextEncoder().encode(`${header}.${claims}`));
      return `${header}.${claims}.${btoa(String.fromCharCode(...new Uint8Array(signature))).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "")}`;
    };
    const envWithKeys = { ...env, accessJwksUrl: `https://access.example.com/certs-${crypto.randomUUID()}` };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({ keys: [{ ...publicJwk, kid: "kid-1", alg: "RS256" }] }))
      .mockResolvedValueOnce(Response.json({ keys: [{ ...secondPublicJwk, kid: "kid-2", alg: "RS256" }] }))
      .mockResolvedValueOnce(Response.json({ keys: [{ ...secondPublicJwk, kid: "kid-2", alg: "RS256" }] }));
    vi.stubGlobal("fetch", fetchMock);
    const validToken = await sign("kid-1");
    const claims = await verifyAccessRequest(new Request("https://dashboard.example.com/api/v1/status", { headers: { "Cf-Access-Jwt-Assertion": validToken } }), envWithKeys);
    expect(claims.email).toBe("owner@example.com");
    for (const invalidToken of [
      await sign("kid-1", { iss: "https://other.example.com" }),
      await sign("kid-1", { aud: "wrong-audience" }),
      await sign("kid-1", { exp: Math.floor(Date.now() / 1000) - 1 }),
      await sign("kid-1", { email: "other@example.com" }),
    ]) {
      await expect(verifyAccessRequest(new Request("https://dashboard.example.com/api/v1/status", { headers: { "Cf-Access-Jwt-Assertion": invalidToken } }), envWithKeys)).rejects.toThrow();
    }
    const [validHeader, validClaims, validSignature] = validToken.split(".");
    const tamperedToken = `${validHeader}.${validClaims}.${validSignature!.startsWith("A") ? "B" : "A"}${validSignature!.slice(1)}`;
    await expect(verifyAccessRequest(new Request("https://dashboard.example.com/api/v1/status", { headers: { "Cf-Access-Jwt-Assertion": tamperedToken } }), envWithKeys)).rejects.toThrow();
    const refreshed = await verifyAccessRequest(new Request("https://dashboard.example.com/api/v1/status", { headers: { "Cf-Access-Jwt-Assertion": await sign("kid-2", { signingKey: secondKeyPair.privateKey }) } }), envWithKeys);
    expect(refreshed.email).toBe("owner@example.com");
    await expect(verifyAccessRequest(new Request("https://dashboard.example.com/api/v1/status", { headers: { "Cf-Access-Jwt-Assertion": await sign("unknown") } }), envWithKeys)).rejects.toThrow();
    expect(fetchMock).toHaveBeenCalledTimes(3);
    const malformedEnv = { ...env, accessJwksUrl: `https://access.example.com/malformed-${crypto.randomUUID()}` };
    const malformedFetch = vi.fn(async () => Response.json({ keys: [{ kid: "bad", kty: "RSA" }] }));
    vi.stubGlobal("fetch", malformedFetch);
    await expect(verifyAccessRequest(new Request("https://dashboard.example.com/api/v1/status", { headers: { "Cf-Access-Jwt-Assertion": await sign("bad") } }), malformedEnv)).rejects.toThrow();
    vi.unstubAllGlobals();
  });
});
