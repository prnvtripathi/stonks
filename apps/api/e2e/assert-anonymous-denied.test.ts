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
 * same script deploy.yml invokes, against two small local HTTP servers that
 * stand in for the two states a deployed environment could be in:
 *
 *  - "healthy": Access (or this application's own accessVerifier) is
 *    correctly rejecting an anonymous request with 401.
 *  - "fail-open": Access has been bypassed/misconfigured and an anonymous
 *    request gets a genuine 200.
 *
 * The script must exit 0 for the first and exit 1 (with output on stderr)
 * for the second -- that's precisely the gate deploy.yml relies on to catch
 * an accidentally-bypassed accessVerifier() or a misconfigured Access
 * application in production.
 */
const execFileAsync = promisify(execFile);
const scriptPath = path.resolve(import.meta.dirname, "../../../scripts/ci/assert-anonymous-denied.sh");

async function listen(status: number): Promise<{ server: Server; url: string }> {
  const server = createServer((_req, res) => {
    res.writeHead(status);
    res.end();
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (address === null || typeof address === "string") throw new Error("Expected a TCP address");
  return { server, url: `http://127.0.0.1:${address.port}` };
}

describe("scripts/ci/assert-anonymous-denied.sh", () => {
  let server: Server | undefined;

  afterEach(async () => {
    if (!server) return;
    await new Promise<void>((resolve, reject) => server!.close((cause) => (cause ? reject(cause) : resolve())));
    server = undefined;
  });

  it("passes (exit 0) when an anonymous request is correctly rejected with 401", async () => {
    const listening = await listen(401);
    server = listening.server;
    await expect(execFileAsync("bash", [scriptPath, listening.url])).resolves.toBeDefined();
  });

  it("fails (exit non-zero) when an anonymous request gets a bare 200 -- the fail-open case", async () => {
    const listening = await listen(200);
    server = listening.server;
    await expect(execFileAsync("bash", [scriptPath, listening.url])).rejects.toMatchObject({ code: 1 });
  });
});
