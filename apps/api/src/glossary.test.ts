import { describe, expect, it } from "vitest";
import { createApi, createTestAccessVerifier, MemoryResearchStore, type ApiEnv } from "./index";

const env: ApiEnv = { accessTeamDomain: "https://access.example.com", accessAudience: "audience", allowedEmails: ["owner@example.com"], allowedOrigin: "https://dashboard.example.com", maxBodyBytes: 16_384, activeDatasetId: "dataset-1" };
const request = (path: string, token: string) => new Request(`https://dashboard.example.com${path}`, { headers: { Authorization: `Bearer ${token}` } });
async function setup() { const api = createApi({ env, store: new MemoryResearchStore(), accessVerifier: createTestAccessVerifier("test-secret"), testAccessSecret: "test-secret" }); return { api, token: await api.issueTestToken({ email: "owner@example.com" }) }; }

describe("glossary API", () => {
  it("lists curated entries and searches aliases without external fetching", async () => {
    const { api, token } = await setup(); const response = await api.fetch(request("/api/v1/glossary?q=RS%20rating&limit=10", token));
    expect(response.status).toBe(200); const body = await response.json() as { data: { slug: string }[]; pagination: { total: number } };
    expect(body.data.map((entry) => entry.slug)).toContain("relative-strength-rating"); expect(body.pagination.total).toBeGreaterThan(0);
  });
  it("returns one full entry and applies safe query bounds", async () => {
    const { api, token } = await setup(); const detail = await api.fetch(request("/api/v1/glossary/nav", token));
    expect(detail.status).toBe(200); expect(await detail.json()).toMatchObject({ slug: "nav", formula: { provenance: expect.any(Array) }, sources: expect.any(Array) });
    expect((await api.fetch(request("/api/v1/glossary?limit=0", token))).status).toBe(400); expect((await api.fetch(request("/api/v1/glossary/missing", token))).status).toBe(404);
  });
});
