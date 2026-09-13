import { createServer, type Server } from "node:http";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";

/**
 * TDD coverage for scripts/ci/assert-anonymous-denied.sh -- the fail-open
 * detector added to .github/workflows/deploy.yml's smoke-test steps (see
 * that file's own header comment for the full rationale). A workflow YAML
 * step cannot be unit-tested directly, so this test exercises the exact
 * same script deploy.yml invokes, against small local HTTP servers (and one
 * closed port, for a transport failure) that stand in for the states a
 * deployed environment could be in.
 *
 * R12/F15 hardening: the script must distinguish genuine denial evidence
 * (401, 403, or a redirect to Access's own hosted login page) from an
 * inconclusive or failed probe (a bare 200 -- fail-open -- but also 404,
 * 5xx, an unrelated redirect, or a transport failure like connection
 * refused or a timeout). Only the former set may exit 0; everything else,
 * including outcomes the earlier version of this script wrongly treated as
 * "denied," must exit non-zero.
 */
const execFileAsync = promisify(execFile);
const scriptPath = path.resolve(import.meta.dirname, "../../../scripts/ci/assert-anonymous-denied.sh");

async function listen(handler: (req: import("node:http").IncomingMessage, res: import("node:http").ServerResponse) => void): Promise<{ server: Server; url: string }> {
  const server = createServer(handler);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (address === null || typeof address === "string") throw new Error("Expected a TCP address");
  return { server, url: `http://127.0.0.1:${address.port}` };
}
const withStatus = (status: number) => (_req: import("node:http").IncomingMessage, res: import("node:http").ServerResponse) => { res.writeHead(status); res.end(); };
const withRedirect = (status: number, location: string) => (_req: import("node:http").IncomingMessage, res: import("node:http").ServerResponse) => { res.writeHead(status, { Location: location }); res.end(); };

describe("scripts/ci/assert-anonymous-denied.sh", () => {
  let server: Server | undefined;

  afterEach(async () => {
    if (!server) return;
    await new Promise<void>((resolve, reject) => server!.close((cause) => (cause ? reject(cause) : resolve())));
    server = undefined;
  });

  it("passes (exit 0) when an anonymous request is correctly rejected with 401", async () => {
    const listening = await listen(withStatus(401));
    server = listening.server;
    await expect(execFileAsync("bash", [scriptPath, listening.url])).resolves.toBeDefined();
  });

  it("passes (exit 0) when an anonymous request is correctly rejected with 403", async () => {
    const listening = await listen(withStatus(403));
    server = listening.server;
    await expect(execFileAsync("bash", [scriptPath, listening.url])).resolves.toBeDefined();
  });

  it("passes (exit 0) when an anonymous request is redirected to Access's hosted login page", async () => {
    const listening = await listen(withRedirect(302, "https://example.cloudflareaccess.com/cdn-cgi/access/login/abc123"));
    server = listening.server;
    await expect(execFileAsync("bash", [scriptPath, listening.url])).resolves.toBeDefined();
  });

  it("fails (exit non-zero) when an anonymous request gets a bare 200 -- the fail-open case", async () => {
    const listening = await listen(withStatus(200));
    server = listening.server;
    await expect(execFileAsync("bash", [scriptPath, listening.url])).rejects.toMatchObject({ code: 1 });
  });

  it("fails (exit non-zero) on an unrelated 404 -- not proof of denial", async () => {
    const listening = await listen(withStatus(404));
    server = listening.server;
    await expect(execFileAsync("bash", [scriptPath, listening.url])).rejects.toMatchObject({ code: 1 });
  });

  it("fails (exit non-zero) on a 500 -- a broken deployment is not proof of denial", async () => {
    const listening = await listen(withStatus(500));
    server = listening.server;
    await expect(execFileAsync("bash", [scriptPath, listening.url])).rejects.toMatchObject({ code: 1 });
  });

  it("fails (exit non-zero) on a redirect that does not point at Access's hosted login", async () => {
    const listening = await listen(withRedirect(302, "https://example.com/some-other-page"));
    server = listening.server;
    await expect(execFileAsync("bash", [scriptPath, listening.url])).rejects.toMatchObject({ code: 1 });
  });

  it("fails (exit non-zero) on connection refused -- a transport failure is not proof of denial", async () => {
    const listening = await listen(withStatus(200));
    server = listening.server;
    const url = listening.url;
    await new Promise<void>((resolve, reject) => listening.server.close((cause) => (cause ? reject(cause) : resolve())));
    server = undefined;
    await expect(execFileAsync("bash", [scriptPath, url])).rejects.toMatchObject({ code: 1 });
  });

  it("fails (exit non-zero) on a request timeout -- an unresponsive target is not proof of denial", async () => {
    const listening = await listen((_req, _res) => { /* never respond, forcing the client's max-time to elapse */ });
    server = listening.server;
    await expect(execFileAsync("bash", [scriptPath, listening.url, "1"])).rejects.toMatchObject({ code: 1 });
  }, 10_000);
});
