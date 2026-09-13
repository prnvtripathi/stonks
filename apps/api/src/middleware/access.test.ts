import { describe, expect, it } from "vitest";
import { createApi, createTestAccessVerifier, MemoryResearchStore, type ApiEnv } from "../index";

/**
 * Regression coverage for R12/F15: a scoped Cloudflare Access service-token
 * identity that can smoke-test `GET /api/v1/status` with a real 200, but can
 * never be treated as an owner email and can never authorize a mutation.
 *
 * Cloudflare Access service-token JWTs carry a `common_name` claim instead
 * of `email`. These tests exercise that path through the same test-double
 * pattern the rest of this codebase uses for Access verification
 * (`createTestAccessVerifier` / `issueTestAccessToken`, HS256-signed local
 * tokens standing in for the real RS256 Access JWT), per this task's brief:
 * "follow this existing test-double pattern ... rather than inventing a
 * different test approach."
 */
const env: ApiEnv = {
  accessTeamDomain: "https://access.example.com",
  accessAudience: "audience",
  allowedEmails: ["owner@example.com"],
  allowedServiceTokenNames: ["ci-deploy-smoke"],
  allowedOrigin: "https://dashboard.example.com",
  maxBodyBytes: 16_384,
  activeDatasetId: "dataset-1",
};

const secret = "test-secret";
const testApi = () => createApi({ env, store: new MemoryResearchStore(), accessVerifier: createTestAccessVerifier(secret), testAccessSecret: secret });

function statusRequest(token: string): Request {
  return new Request("https://dashboard.example.com/api/v1/status", { headers: { Authorization: `Bearer ${token}` } });
}
function mutationRequest(token: string): Request {
  return new Request("https://dashboard.example.com/api/v1/screens", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", Origin: env.allowedOrigin },
    body: JSON.stringify({ name: "x", source: "Volume > 1" }),
  });
}

describe("scoped Access service-token identity", () => {
  it("smoke-checks GET /api/v1/status with a genuine 200 for the exact allowlisted identity", async () => {
    const api = testApi();
    const token = await api.issueTestToken({ commonName: "ci-deploy-smoke" });
    const response = await api.fetch(statusRequest(token));
    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toHaveProperty("datasetId");
  });

  it("rejects a service token whose audience does not match", async () => {
    const api = testApi();
    const token = await api.issueTestToken({ commonName: "ci-deploy-smoke", aud: "wrong-audience" });
    const response = await api.fetch(statusRequest(token));
    expect(response.status).toBe(401);
  });

  it("rejects an expired service token", async () => {
    const api = testApi();
    const token = await api.issueTestToken({ commonName: "ci-deploy-smoke", exp: Math.floor(Date.now() / 1000) - 60 });
    const response = await api.fetch(statusRequest(token));
    expect(response.status).toBe(401);
  });

  it("rejects a service token whose common_name is not on the allowlist", async () => {
    const api = testApi();
    const token = await api.issueTestToken({ commonName: "some-other-service" });
    const response = await api.fetch(statusRequest(token));
    expect(response.status).toBe(401);
  });

  it("denies a mutation attempted with an otherwise-valid, allowlisted service token (403, not treated as owner)", async () => {
    const api = testApi();
    const token = await api.issueTestToken({ commonName: "ci-deploy-smoke" });
    const response = await api.fetch(mutationRequest(token));
    expect(response.status).toBe(403);
    // Confirm the mutation never actually ran.
    const list = await api.fetch(new Request("https://dashboard.example.com/api/v1/screens", { headers: { Authorization: `Bearer ${await api.issueTestToken({ email: "owner@example.com" })}` } }));
    const listed = (await list.json()) as { readonly data: readonly unknown[] };
    expect(listed.data).toHaveLength(0);
  });

  it("never authorizes a service-token identity as an owner email on a non-status GET route", async () => {
    const api = testApi();
    const token = await api.issueTestToken({ commonName: "ci-deploy-smoke" });
    const response = await api.fetch(new Request("https://dashboard.example.com/api/v1/metrics", { headers: { Authorization: `Bearer ${token}` } }));
    expect(response.status).toBe(403);
  });

  it("still rejects a service token when no service-token allowlist is configured", async () => {
    const { allowedServiceTokenNames: _omit, ...rest } = env;
    const bareEnv: ApiEnv = rest;
    const api = createApi({ env: bareEnv, store: new MemoryResearchStore(), accessVerifier: createTestAccessVerifier(secret), testAccessSecret: secret });
    const token = await api.issueTestToken({ commonName: "ci-deploy-smoke" });
    const response = await api.fetch(statusRequest(token));
    expect(response.status).toBe(401);
  });
});
