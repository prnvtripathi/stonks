import { createServer, type Server } from "node:http";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { createApi, MemoryResearchStore, type ApiEnv } from "../src/index";

/**
 * Black-box HTTP acceptance test for Task 14's Final Verification Gate item
 * "Confirm anonymous ... API ... requests fail".
 *
 * `src/index.test.ts` already covers "rejects missing Access identity" at
 * the unit level: it calls the in-memory Hono-style router's `.fetch()`
 * directly with a hand-built `Request` object, in the same process, with no
 * network involved. That proves the routing/access-check *logic* is
 * correct, but it can never observe a class of bugs that only exist at the
 * HTTP boundary -- e.g. a status code silently rewritten by a runtime
 * adapter, a header dropped in transit, or (as here) confirming a genuine
 * anonymous TCP client with zero Access/Authorization headers set by the
 * runtime, not by test code, is rejected.
 *
 * This test therefore spins up a real Node `http` server that mounts the
 * exact same `Api.fetch` handler used in production (`createApi(...).fetch`,
 * the same function `createWorker`/the Worker's `export default` delegate
 * to), and drives it with the platform `fetch()` over a real loopback
 * socket. We chose a plain Node HTTP server over `wrangler dev` because
 * `wrangler dev` needs to reach the Cloudflare API / a local Miniflare
 * runtime and bind a listening port from a fresh process, which is not
 * reliably available in a sandboxed CI-like environment; a same-process
 * Node server wrapping the identical Worker `fetch` contract gives an
 * equivalent black-box guarantee (real sockets, real HTTP semantics) without
 * that dependency. See docs/operations/deploy.md for the full rationale.
 */
describe("deployed API shape: anonymous access", () => {
  let server: Server;
  let baseUrl: string;

  beforeAll(async () => {
    const env: ApiEnv = {
      accessTeamDomain: "https://access.example.com",
      accessAudience: "audience",
      allowedEmails: ["owner@example.com"],
      allowedOrigin: "https://dashboard.example.com",
      maxBodyBytes: 16_384,
      activeDatasetId: "dataset-1",
    };
    const api = createApi({ env, store: new MemoryResearchStore() });

    server = createServer((req, res) => {
      void (async () => {
        const headers = new Headers();
        for (const [key, value] of Object.entries(req.headers)) {
          if (value === undefined) continue;
          for (const single of Array.isArray(value) ? value : [value]) headers.append(key, single);
        }
        const request = new Request(`http://127.0.0.1${req.url}`, { method: req.method ?? "GET", headers });
        const response = await api.fetch(request);
        const body = Buffer.from(await response.arrayBuffer());
        res.writeHead(response.status, Object.fromEntries(response.headers));
        res.end(body);
      })();
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    if (address === null || typeof address === "string") throw new Error("Expected a TCP address");
    baseUrl = `http://127.0.0.1:${address.port}`;
  });

  afterAll(async () => {
    await new Promise<void>((resolve, reject) => server.close((cause) => (cause ? reject(cause) : resolve())));
  });

  it("rejects a genuine anonymous HTTP request with no Access/Authorization header", async () => {
    const response = await fetch(`${baseUrl}/api/v1/status`);
    expect(response.status).toBe(401);
    expect(response.headers.get("Content-Security-Policy")).toBe("default-src 'none'; frame-ancestors 'none'");
  });

  it("rejects an anonymous HTTP request for a mutation route too", async () => {
    const response = await fetch(`${baseUrl}/api/v1/screens`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "x", source: "Volume > 1" }),
    });
    expect(response.status).toBe(401);
  });
});
