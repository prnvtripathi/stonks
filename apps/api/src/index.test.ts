import { describe, expect, it, vi } from "vitest";
import worker from "./index";
import { createApi, createTestAccessVerifier, D1ResearchStore, MemoryResearchStore, verifyAccessRequest, type ApiEnv, type D1Database, type D1Result, type D1Statement } from "./index";
import { buildExplanation } from "./repositories";
import { createSqliteD1, seedDataset, seedScreen } from "./test-support/sqlite-d1";

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

const workerBindings: Record<string, unknown> = {
  ACCESS_TEAM_DOMAIN: "access.example.com",
  ACCESS_AUD: "audience",
  ACCESS_ALLOWED_EMAILS: "owner@example.com",
  ALLOWED_ORIGIN: "https://dashboard.example.com",
  MAX_BODY_BYTES: 16_384,
};

describe("worker bindings", () => {
  it("returns 503 instead of serving fabricated data when the D1 binding is missing", async () => {
    const response = await worker.fetch(request("/api/v1/status"), { ...workerBindings });
    expect(response.status).toBe(503);
    expect((await response.json() as { error: { code: string } }).error.code).toBe("DATA_STORE_IS_NOT_CONFIGURED");
    expect(response.headers.get("Content-Security-Policy")).toBe("default-src 'none'; frame-ancestors 'none'");
  });

  it("returns 503 when the D1 binding is present but not a D1 database", async () => {
    const response = await worker.fetch(request("/api/v1/status"), { ...workerBindings, DB: { notPrepare: true } });
    expect(response.status).toBe(503);
  });

  it("serves the real D1-backed store when the binding is valid", async () => {
    const response = await worker.fetch(request("/api/v1/status"), { ...workerBindings, DB: { prepare: () => ({}) } });
    // Anonymous, so Access rejects it -- but it is a 401, not a 503, which
    // proves the store was constructed from the real binding.
    expect(response.status).toBe(401);
  });
});

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

  it("sets strict security response headers on every response, without weakening existing cache directives", async () => {
    const api = testApi();
    const unauthenticated = await api.fetch(request("/api/v1/status"));
    expect(unauthenticated.status).toBe(401);
    expect(unauthenticated.headers.get("Content-Security-Policy")).toBe("default-src 'none'; frame-ancestors 'none'");
    expect(unauthenticated.headers.get("Strict-Transport-Security")).toBe("max-age=63072000; includeSubDomains; preload");
    expect(unauthenticated.headers.get("X-Frame-Options")).toBe("DENY");
    expect(unauthenticated.headers.get("X-Content-Type-Options")).toBe("nosniff");
    expect(unauthenticated.headers.get("Referrer-Policy")).toBe("no-referrer");

    const authenticated = await api.fetch(request("/api/v1/status", { headers: { Authorization: `Bearer ${await token()}` } }));
    expect(authenticated.headers.get("Cache-Control")).toBe("private, no-store");
    expect(authenticated.headers.get("Content-Security-Policy")).toBe("default-src 'none'; frame-ancestors 'none'");
    expect(authenticated.headers.get("Strict-Transport-Security")).toBe("max-age=63072000; includeSubDomains; preload");
    expect(authenticated.headers.get("X-Frame-Options")).toBe("DENY");
    expect(authenticated.headers.get("X-Content-Type-Options")).toBe("nosniff");
    expect(authenticated.headers.get("Referrer-Policy")).toBe("no-referrer");
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
    const matchesFor = (run: { readonly id: string; readonly datasetId: string }) => store.runs.get(`${run.datasetId}:${screen.id}`)!.find((candidate) => candidate.id === run.id)!.matches;
    const flags = (run: { readonly id: string; readonly datasetId: string }) => Object.fromEntries(matchesFor(run).map((match) => [match.instrumentId, { entered: match.entered, exited: match.exited }]));

    expect(flags(first)).toEqual({ one: { entered: true, exited: false } });
    expect(flags(second)).toEqual({ two: { entered: true, exited: false }, one: { entered: false, exited: true } });
    expect(flags(third)).toEqual({ one: { entered: true, exited: false }, two: { entered: false, exited: true } });
    expect(matchesFor(second).find((match) => match.instrumentId === "one")?.exited).toBe(true);
    expect(matchesFor(third).find((match) => match.instrumentId === "two")?.exited).toBe(true);
    expect((await store.listRuns(screen.id)).runs.map((run) => run.effectiveDate)).toEqual(["2026-03-01", "2026-02-01", "2026-01-01"]);
  });

  it("does not repeat an exit after an instrument remains absent", async () => {
    const store = new MemoryResearchStore("dataset-1");
    store.instruments.set("one", { instrumentId: "one", symbol: "ONE", name: "One", assetClass: "equity", active: true, metrics: { volume: 10 } });
    const screen = await store.createScreen({ name: "Volume", source: "Volume > 1", languageVersion: "v1", createdAt: "2026-01-01", updatedAt: "2026-01-01" });
    await store.runScreen("dataset-1", screen, "2026-01-01");
    store.instruments.set("one", { instrumentId: "one", symbol: "ONE", name: "One", assetClass: "equity", active: false, metrics: { volume: 10 } });
    const second = await store.runScreen("dataset-1", screen, "2026-01-02");
    const third = await store.runScreen("dataset-1", screen, "2026-01-03");
    const matchesFor = (run: { readonly id: string; readonly datasetId: string }) => store.runs.get(`${run.datasetId}:${screen.id}`)!.find((candidate) => candidate.id === run.id)!.matches;

    expect(matchesFor(second).map((match) => ({ instrumentId: match.instrumentId, exited: match.exited }))).toEqual([{ instrumentId: "one", exited: true }]);
    expect(matchesFor(third).filter((match) => !match.exited)).toEqual([]);
    expect(second.matchCount).toBe(0);
    expect(third.matchCount).toBe(0);
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

  it("keeps an explicit saved run immutable across a publication and screen edit", async () => {
    const store = new MemoryResearchStore("dataset-a");
    store.instruments.set("old", { instrumentId: "old", symbol: "OLD_SYMBOL", name: "Old company", assetClass: "equity", active: true, metrics: { volume: 200 } });
    const api = createApi({ env, store, now: () => new Date("2026-09-09T12:00:00.000Z"), accessVerifier: createTestAccessVerifier("test-secret"), testAccessSecret: "test-secret" });
    const bearer = await api.issueTestToken({ email: "owner@example.com" });
    const headers = { Authorization: `Bearer ${bearer}`, "Content-Type": "application/json", Origin: env.allowedOrigin };
    const created = await api.fetch(request("/api/v1/screens", { method: "POST", headers, body: JSON.stringify({ name: "Volume", source: "Volume > 100" }) }));
    const screen = await created.json() as { id: string };
    const runResponse = await api.fetch(request(`/api/v1/screens/${screen.id}/runs`, { method: "POST", headers }));
    const oldRun = await runResponse.json() as { id: string; effectiveDate: string; completedAt: string };

    store.setActiveDataset("dataset-b");
    store.instruments.clear();
    await api.fetch(request(`/api/v1/screens/${screen.id}`, { method: "PUT", headers, body: JSON.stringify({ name: "Volume", source: "Volume > 1000" }) }));

    const response = await api.fetch(request(`/api/v1/screens/${screen.id}/results?runId=${oldRun.id}`, { headers }));
    expect(response.status).toBe(200);
    const body = await response.json() as { screen: { source: string }; run: { datasetId: string; effectiveDate: string; completedAt: string; source: string | null; languageVersion: string | null; isCurrentDataset: boolean; isCurrentQuery: boolean; matches: { symbol: string | null; name: string | null; assetClass: string | null }[] } };
    expect(body.screen.source).toBe("Volume > 1000");
    expect(body.run).toMatchObject({ datasetId: "dataset-a", effectiveDate: "2026-09-04", completedAt: "2026-09-09T12:00:00.000Z", source: "Volume > 100", languageVersion: "v1", isCurrentDataset: false, isCurrentQuery: false });
    expect(body.run.matches[0]).toMatchObject({ symbol: "OLD_SYMBOL", name: "Old company", assetClass: "equity" });
  });

  it("uses the dataset effective date rather than the execution date for new runs", async () => {
    const store = new MemoryResearchStore("dataset-a");
    store.instruments.set("one", { instrumentId: "one", symbol: "ONE", name: "One", assetClass: "equity", active: true, metrics: { volume: 2 } });
    const api = createApi({ env, store, now: () => new Date("2026-09-09T12:00:00.000Z"), accessVerifier: createTestAccessVerifier("test-secret"), testAccessSecret: "test-secret" });
    const bearer = await api.issueTestToken({ email: "owner@example.com" });
    const headers = { Authorization: `Bearer ${bearer}`, "Content-Type": "application/json", Origin: env.allowedOrigin };
    const created = await api.fetch(request("/api/v1/screens", { method: "POST", headers, body: JSON.stringify({ name: "Volume", source: "Volume > 1" }) }));
    const screen = await created.json() as { id: string };
    const response = await api.fetch(request(`/api/v1/screens/${screen.id}/runs`, { method: "POST", headers }));
    expect(response.status).toBe(201);
    expect(await response.json()).toMatchObject({ effectiveDate: "2026-09-04", completedAt: "2026-09-09T12:00:00.000Z" });
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
    const page = await store.pageRunMatches(run.id, { sort: "rank", direction: "asc", limit: 10, offset: 0 });
    const clauses = page.matches[0]?.explanation.clauses ?? [];
    expect(clauses).toHaveLength(2);
    expect(clauses.map((clause) => clause.metric)).toEqual(["volume", "return_3m"]);
    expect(clauses.map((clause) => clause.result)).toEqual(["Matched", "Unavailable"]);
    expect(clauses[1]?.clause).toContain("OR");
    expect(clauses.map((clause) => clause.metric)).not.toContain("roe");
  });

  it("orders same-day runs by completion time before the stable ID", async () => {
    const store = new MemoryResearchStore("dataset-1");
    store.runs.set("dataset-1:screen-1", [
      { id: "z-uuid", screenId: "screen-1", datasetId: "dataset-1", effectiveDate: "2026-09-07", completedAt: "2026-09-07T10:00:00.000Z", matchCount: 0, status: "complete", source: null, languageVersion: null, matches: [] },
      { id: "a-uuid", screenId: "screen-1", datasetId: "dataset-1", effectiveDate: "2026-09-07", completedAt: "2026-09-07T11:00:00.000Z", matchCount: 0, status: "complete", source: null, languageVersion: null, matches: [] },
    ]);
    expect((await store.listRuns("screen-1")).runs.map((run) => run.id)).toEqual(["a-uuid", "z-uuid"]);
  });

  it("uses the most recently completed run for default results and transitions", async () => {
    const store = new MemoryResearchStore("dataset-1");
    const screen = await store.createScreen({ name: "Volume", source: "Volume > 1", languageVersion: "v1", createdAt: "2026-01-01", updatedAt: "2026-01-01" });
    store.instruments.set("old", { instrumentId: "old", symbol: "OLD", name: "Old", assetClass: "equity", active: true, metrics: { volume: 2 } });
    await store.runScreen("dataset-1", screen, "2026-09-10", "2026-09-09T10:00:00.000Z");
    store.instruments.set("old", { instrumentId: "old", symbol: "OLD", name: "Old", assetClass: "equity", active: false, metrics: { volume: 2 } });
    store.instruments.set("latest", { instrumentId: "latest", symbol: "LATEST", name: "Latest", assetClass: "equity", active: true, metrics: { volume: 2 } });
    const latest = await store.runScreen("dataset-1", screen, "2026-09-04", "2026-09-09T11:00:00.000Z");

    expect((await store.listRuns(screen.id)).runs[0]?.id).toBe(latest.id);

    const next = await store.runScreen("dataset-1", screen, "2026-09-03", "2026-09-09T12:00:00.000Z");
    const nextPage = await store.pageRunMatches(next.id, { sort: "rank", direction: "asc", limit: 10, offset: 0 });
    expect(nextPage.matches.find((match) => match.instrumentId === "latest")).toMatchObject({ entered: false, exited: false });
  });

  it("inverts only known predicate outcomes for odd NOT parity", () => {
    const item = { instrumentId: "ONE", symbol: "ONE", name: "One", assetClass: "equity" as const, active: true, metricRows: [{ metric: "volume", value: 3, state: "present" as const }] };
    expect(buildExplanation("NOT Volume > 5", item).clauses?.[0]?.result).toBe("Matched");
    expect(buildExplanation("NOT Volume > 5", { ...item, metricRows: [{ metric: "volume", value: 7, state: "present" as const }] }).clauses?.[0]?.result).toBe("Not matched");
  });

  it("preserves unknown states through NOT and restores the base result through double NOT", () => {
    const missing = { instrumentId: "ONE", symbol: "ONE", name: "One", assetClass: "equity" as const, active: true, metricRows: [{ metric: "volume", value: null, state: "missing" as const }] };
    const notApplicable = { ...missing, metricRows: [{ metric: "volume", value: null, state: "not_applicable" as const }] };
    expect(buildExplanation("NOT Volume > 5", missing).clauses?.[0]?.result).toBe("Unavailable");
    expect(buildExplanation("NOT Volume > 5", notApplicable).clauses?.[0]?.result).toBe("Not applicable");
    expect(buildExplanation("NOT NOT Volume > 5", { ...missing, metricRows: [{ metric: "volume", value: 3, state: "present" as const }] }).clauses?.[0]?.result).toBe("Not matched");
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

  it("excludes exited rows from a page and reports the persisted ordinal as rank", async () => {
    const { db, sqlite } = createSqliteD1();
    seedDataset(sqlite, "dataset-1");
    seedScreen(sqlite, "screen-1", "Volume", "Volume > 1");
    sqlite.prepare("INSERT INTO screen_runs (dataset_id, run_id, screen_id, effective_date, status, result_count, source, language_version, completed_at) VALUES (?,?,?,?,?,?,?,?,?)").run("dataset-1", "run-1", "screen-1", "2026-09-07", "complete", 1, "Volume > 1", "v1", "2026-09-07T00:00:00.000Z");
    sqlite.prepare("INSERT INTO screen_matches (dataset_id, run_id, ordinal, instrument_id, score, symbol, name, asset_class, metrics_json, entered, exited) VALUES (?,?,?,?,?,?,?,?,?,?,?)").run("dataset-1", "run-1", 1, "one", 2, "ONE", "One", "equity", "[]", 1, 0);
    sqlite.prepare("INSERT INTO screen_matches (dataset_id, run_id, ordinal, instrument_id, score, symbol, name, asset_class, metrics_json, entered, exited) VALUES (?,?,?,?,?,?,?,?,?,?,?)").run("dataset-1", "run-1", 2, "two", 1, "TWO", "Two", "equity", "[]", 0, 1);
    const store = new D1ResearchStore(db);
    const { runs: [run] } = await store.listRuns("screen-1");
    expect(run).toMatchObject({ id: "run-1", matchCount: 1 });
    const page = await store.pageRunMatches("run-1", { sort: "rank", direction: "asc", limit: 10, offset: 0 });
    expect(page.total).toBe(1);
    expect(page.matches.map((match) => ({ instrumentId: match.instrumentId, rank: match.rank, exited: match.exited }))).toEqual([
      { instrumentId: "one", rank: 1, exited: false },
    ]);
  });

  it("orders D1 runs by parsed execution time for defaults and predecessors", async () => {
    const { db, sqlite } = createSqliteD1();
    seedDataset(sqlite, "dataset-1");
    seedScreen(sqlite, "screen-1", "Volume", "Volume > 1");
    sqlite.prepare("INSERT INTO instrument_snapshots (instrument_id, dataset_id, symbol, name, asset_class, active, metric_values_json, metric_rows_json) VALUES ('latest','dataset-1','LATEST','Latest','equity',1,?,?)").run(JSON.stringify({ volume: 2, momentum_score: 2 }), JSON.stringify([{ metric: "volume", value: 2, state: "present" }]));
    const run = (runId: string, effectiveDate: string, completedAt: string | null) => sqlite.prepare("INSERT INTO screen_runs (dataset_id, run_id, screen_id, effective_date, status, result_count, source, language_version, completed_at) VALUES (?,?,?,?,?,?,?,?,?)").run("dataset-1", runId, "screen-1", effectiveDate, "complete", 1, "Volume > 1", "v1", completedAt);
    run("text", "2026-09-10", "2026-09-09T12:00:00Z");
    run("fractional", "2026-09-04", "2026-09-09T12:00:00.500Z");
    run("missing", "2026-09-30", null);
    run("invalid", "2026-09-29", "not-a-time");
    sqlite.prepare("INSERT INTO screen_matches (dataset_id, run_id, ordinal, instrument_id, score, symbol, name, asset_class, metrics_json, entered, exited) VALUES ('dataset-1','text',1,'old',1,'OLD','Old','equity','[]',1,0)").run();
    sqlite.prepare("INSERT INTO screen_matches (dataset_id, run_id, ordinal, instrument_id, score, symbol, name, asset_class, metrics_json, entered, exited) VALUES ('dataset-1','fractional',1,'latest',2,'LATEST','Latest','equity','[]',1,0)").run();
    const store = new D1ResearchStore(db);
    const api = createApi({ env, store, accessVerifier: createTestAccessVerifier("test-secret"), testAccessSecret: "test-secret" });
    const bearer = await api.issueTestToken({ email: "owner@example.com" });
    const response = await api.fetch(request("/api/v1/screens/screen-1/results", { headers: { Authorization: `Bearer ${bearer}` } }));
    expect(response.status).toBe(200);
    // "fractional" completed 500ms after "text" -- the fractional timestamp must win the default-run tiebreak.
    expect((await response.json() as { run: { id: string } }).run.id).toBe("fractional");

    const screen = { id: "screen-1", name: "Volume", source: "Volume > 1", languageVersion: "v1", createdAt: "2026-01-01", updatedAt: "2026-01-01" } as const;
    const next = await store.runScreen("dataset-1", screen, "2026-09-03", "2026-09-09T12:00:01.000Z");
    const nextPage = await store.pageRunMatches(next.id, { sort: "rank", direction: "asc", limit: 10, offset: 0 });
    expect(nextPage.matches.find((match) => match.instrumentId === "latest")).toMatchObject({ entered: false, exited: false });
  });

  it("does not repeat a persisted exit after an instrument remains absent", async () => {
    const { db, sqlite } = createSqliteD1();
    seedDataset(sqlite, "dataset-1");
    seedScreen(sqlite, "screen-1", "x", "Volume > 1");
    const upsertInstrument = (present: boolean) => {
      sqlite.prepare("DELETE FROM instrument_snapshots WHERE instrument_id = 'one'").run();
      if (present) sqlite.prepare("INSERT INTO instrument_snapshots (instrument_id, dataset_id, symbol, name, asset_class, active, metric_values_json, metric_rows_json) VALUES ('one','dataset-1','ONE','One','equity',1,?,?)").run(JSON.stringify({ volume: 10, momentum_score: 10 }), JSON.stringify([{ metric: "volume", value: 10, state: "present" }]));
    };
    upsertInstrument(true);
    const store = new D1ResearchStore(db);
    const screen = { id: "screen-1", name: "x", source: "Volume > 1", languageVersion: "v1", createdAt: "2026-01-01", updatedAt: "2026-01-01" } as const;
    await store.runScreen("dataset-1", screen, "2026-01-01");
    upsertInstrument(false);
    const second = await store.runScreen("dataset-1", screen, "2026-01-02");
    const third = await store.runScreen("dataset-1", screen, "2026-01-03");

    expect(second.matchCount).toBe(0);
    expect(third.matchCount).toBe(0);
    const secondRows = sqlite.prepare("SELECT instrument_id, exited FROM screen_matches WHERE run_id = ?").all(second.id) as { instrument_id: string; exited: number }[];
    expect(secondRows.map((row) => ({ instrumentId: row.instrument_id, exited: Boolean(row.exited) }))).toEqual([{ instrumentId: "one", exited: true }]);
    const thirdExitedRows = sqlite.prepare("SELECT instrument_id FROM screen_matches WHERE run_id = ? AND exited = 1").all(third.id) as { instrument_id: string }[];
    expect(thirdExitedRows).toEqual([]);
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
    expect(statements.some((sql) => sql.includes("INSERT INTO screen_runs"))).toBe(false);
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
