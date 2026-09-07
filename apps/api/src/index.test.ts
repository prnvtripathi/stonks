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

  it("rejects a match-write failure without leaving a complete run", async () => {
    const statements: string[] = [];
    class FailingMatchStatement implements D1Statement {
      private sql = "";
      bind(..._values: unknown[]): D1Statement { return this; }
      async first<T extends Record<string, unknown> = Record<string, unknown>>(): Promise<T | null> { return null; }
      async all<T extends Record<string, unknown> = Record<string, unknown>>(): Promise<{ results: readonly T[] }> {
        if (this.sql.includes("SELECT screen_id")) return { results: [{ screen_id: "screen-1", name: "x", expression: "Volume > 1", created_at: "2026-01-01", updated_at: "2026-01-01" }] as T[] };
        if (this.sql.includes("SELECT run_id")) return { results: [] };
        if (this.sql.includes("SELECT i.instrument_id")) return { results: [{ instrument_id: "one", score: 1 }, { instrument_id: "two", score: 2 }] as T[] };
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
    const sign = async (kid: string, email = "owner@example.com", exp = Math.floor(Date.now() / 1000) + 300, signingKey = keyPair.privateKey) => {
      const header = encode({ alg: "RS256", typ: "JWT", kid });
      const claims = encode({ iss: "https://access.example.com", aud: "audience", email, exp });
      const signature = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", signingKey, new TextEncoder().encode(`${header}.${claims}`));
      return `${header}.${claims}.${btoa(String.fromCharCode(...new Uint8Array(signature))).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "")}`;
    };
    const envWithKeys = { ...env, accessJwksUrl: `https://access.example.com/certs-${crypto.randomUUID()}` };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({ keys: [{ ...publicJwk, kid: "kid-1", alg: "RS256" }] }))
      .mockResolvedValueOnce(Response.json({ keys: [{ ...secondPublicJwk, kid: "kid-2", alg: "RS256" }] }))
      .mockResolvedValueOnce(Response.json({ keys: [{ ...secondPublicJwk, kid: "kid-2", alg: "RS256" }] }));
    vi.stubGlobal("fetch", fetchMock);
    const claims = await verifyAccessRequest(new Request("https://dashboard.example.com/api/v1/status", { headers: { "Cf-Access-Jwt-Assertion": await sign("kid-1") } }), envWithKeys);
    expect(claims.email).toBe("owner@example.com");
    await expect(verifyAccessRequest(new Request("https://dashboard.example.com/api/v1/status", { headers: { "Cf-Access-Jwt-Assertion": await sign("kid-1", "other@example.com") } }), envWithKeys)).rejects.toThrow();
    const refreshed = await verifyAccessRequest(new Request("https://dashboard.example.com/api/v1/status", { headers: { "Cf-Access-Jwt-Assertion": await sign("kid-2", "owner@example.com", undefined, secondKeyPair.privateKey) } }), envWithKeys);
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
