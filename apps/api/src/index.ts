import { DEFAULT_METRIC_CATALOG } from "@stonks/contracts";
import { parseQuery, typecheckQuery } from "@stonks/query";
import { type ApiEnv } from "./env";
import { AccessError, issueTestAccessToken, verifyAccessRequest, type AccessVerifier } from "./middleware/access";
import { readApiEnv } from "./env";
import { D1ResearchStore, MemoryResearchStore, type ResearchStore } from "./repositories";

export * from "./env";
export * from "./middleware/access";
export * from "./repositories";

export interface ApiDependencies { readonly env: ApiEnv; readonly store: ResearchStore; readonly now?: () => Date; readonly accessVerifier?: AccessVerifier; readonly testAccessSecret?: string; }
export interface Api { fetch(request: Request): Promise<Response>; issueTestToken(options: { readonly email: string; readonly exp?: number; readonly aud?: string }): Promise<string>; }

const jsonHeaders = { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "private, no-store" };
const json = (body: unknown, status = 200): Response => new Response(JSON.stringify(body), { status, headers: jsonHeaders });
const error = (status: number, message: string, details?: unknown): Response => json({ error: { code: message.toUpperCase().replaceAll(" ", "_"), message, ...(details === undefined ? {} : { details }) } }, status);

/**
 * Response-level security headers applied to every response this Worker
 * returns, success or error, authenticated or not. This is a JSON-only API
 * behind Cloudflare Access with no HTML rendering surface of its own, so the
 * CSP is maximally strict (`'none'`) rather than allowlisting any origin.
 * These are added alongside each route's existing `Cache-Control` (private,
 * no-store or private, max-age=...) rather than replacing it -- caching
 * semantics are unrelated to these headers and must not be weakened here.
 */
const SECURITY_HEADERS: Readonly<Record<string, string>> = {
  "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
  "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
  "X-Frame-Options": "DENY",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
};
function withSecurityHeaders(response: Response): Response {
  const headers = new Headers(response.headers);
  for (const [key, value] of Object.entries(SECURITY_HEADERS)) headers.set(key, value);
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}

export function createApi(dependencies: ApiDependencies): Api {
  const { env, store } = dependencies;
  const now = dependencies.now ?? (() => new Date());
  return { fetch: async (request) => withSecurityHeaders(await handle(request, env, store, now, dependencies.accessVerifier ?? verifyAccessRequest)), issueTestToken: (options) => { if (!dependencies.testAccessSecret) throw new Error("Test token issuance requires an injected test secret"); return issueTestAccessToken(env, dependencies.testAccessSecret, options); } };
}

export type WorkerBindings = ApiEnv & { DB?: unknown; CHARTS?: { get(key: string): Promise<{ body: ReadableStream<Uint8Array>; httpMetadata?: { contentType?: string; contentEncoding?: string } } | null> } };

/** Raised when a required Cloudflare binding is absent or the wrong shape. */
export class MissingBindingError extends Error {}

/**
 * Build the production Worker. A missing or malformed `DB` binding is a
 * deployment fault, not a fallback: serving `MemoryResearchStore` here would
 * quietly answer with a fabricated dataset ID and effective date, which is
 * indistinguishable from real data to a caller. Fail closed instead; the
 * in-memory store is reachable only through `createTestWorker`.
 */
export function createWorker(env: WorkerBindings): Api {
  if (!env.DB || typeof (env.DB as { prepare?: unknown }).prepare !== "function") throw new MissingBindingError("Data store is not configured");
  const store = new D1ResearchStore(env.DB as never, env.CHARTS ? { get: async (key) => { const object = await env.CHARTS!.get(key); if (!object) return null; return { body: object.body, ...(object.httpMetadata?.contentType ? { contentType: object.httpMetadata.contentType } : {}), ...(object.httpMetadata?.contentEncoding ? { contentEncoding: object.httpMetadata.contentEncoding } : {}) }; } } : undefined);
  return createApi({ env, store });
}

/** Test-only entry point: the same routing over an explicit in-memory store. */
export function createTestWorker(env: WorkerBindings, store: ResearchStore = new MemoryResearchStore(env.activeDatasetId ?? "dataset-1")): Api {
  return createApi({ env, store });
}

export default { fetch(request: Request, bindings: Record<string, unknown>): Promise<Response> {
  let api: Api;
  try { api = createWorker({ ...readApiEnv(bindings), ...bindings } as WorkerBindings); }
  catch (cause) { if (cause instanceof MissingBindingError) return Promise.resolve(withSecurityHeaders(error(503, cause.message))); throw cause; }
  return api.fetch(request);
} };

async function handle(request: Request, env: ApiEnv, store: ResearchStore, now: () => Date, accessVerifier: AccessVerifier): Promise<Response> {
  const origin = request.headers.get("Origin");
  if (origin && origin !== env.allowedOrigin) return error(403, "Origin is not allowed");
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/$/, "");
  if (request.method === "OPTIONS") {
    const preflightMethods = allowedMethodsFor(path);
    if (!preflightMethods) return error(404, "Not found");
    return new Response(null, { status: 204, headers: { Allow: `${preflightMethods.join(", ")}, OPTIONS` } });
  }
  try { await accessVerifier(request, env); } catch (cause) { return cause instanceof AccessError ? error(cause.status, cause.message) : error(401, "Unauthenticated"); }
  const allowedMethods = allowedMethodsFor(path);
  if (!allowedMethods) return error(404, "Not found");
  if (!allowedMethods.includes(request.method as "GET" | "POST" | "PUT")) return methodNotAllowed(`${allowedMethods.join(", ")}, OPTIONS`);
  if ((request.method === "POST" || request.method === "PUT") && origin !== env.allowedOrigin) return error(403, "Origin is required for mutations");
  try {
    if (path === "/api/v1/status" && request.method === "GET") return json(await store.status(await store.activeDatasetId()));
    if (path === "/api/v1/metrics" && request.method === "GET") return json({ metrics: await store.metrics() });
    if (path === "/api/v1/glossary" && request.method === "GET") return listGlossary(url, store);
    const glossaryDetail = path.match(/^\/api\/v1\/glossary\/([^/]+)$/);
    if (glossaryDetail && request.method === "GET") return getGlossary(decodeURIComponent(glossaryDetail[1]!), store);
    if (path === "/api/v1/instruments" && request.method === "GET") return listInstruments(url, store);
    if (path === "/api/v1/screens" && request.method === "GET") return listScreens(url, store);
    if (path === "/api/v1/screens" && request.method === "POST") return createScreen(request, env, store, now);
    const screenDetail = path.match(/^\/api\/v1\/screens\/([^/]+)$/);
    if (screenDetail && request.method === "GET") return getScreen(decodeURIComponent(screenDetail[1]!), store);
    if (screenDetail && request.method === "PUT") return updateScreen(decodeURIComponent(screenDetail[1]!), request, env, store, now);
    const screenRuns = path.match(/^\/api\/v1\/screens\/([^/]+)\/runs$/);
    if (screenRuns && request.method === "GET") return getRuns(decodeURIComponent(screenRuns[1]!), store);
    if (screenRuns && request.method === "POST") return runScreen(decodeURIComponent(screenRuns[1]!), store, now);
    const screenResults = path.match(/^\/api\/v1\/screens\/([^/]+)\/results$/);
    if (screenResults && request.method === "GET") return getResults(decodeURIComponent(screenResults[1]!), url, store);
    const chart = path.match(/^\/api\/v1\/instruments\/([^/]+)\/chart$/);
    if (chart && request.method === "GET") return getChart(decodeURIComponent(chart[1]!), store);
    const instrument = path.match(/^\/api\/v1\/instruments\/([^/]+)$/);
    if (instrument && request.method === "GET") return getInstrument(decodeURIComponent(instrument[1]!), store);
    return error(404, "Not found");
  } catch { return error(500, "Internal server error"); }
}

async function listScreens(url: URL, store: ResearchStore): Promise<Response> {
  const sort = url.searchParams.get("sort") ?? "updatedAt";
  if (!(new Set(["updatedAt", "name", "createdAt"])).has(sort)) return error(400, "Invalid sort");
  const pagination = readPagination(url);
  if (!pagination) return error(400, "Invalid pagination");
  const { limit, offset } = pagination;
  const screens = await store.listScreens();
  const ordered = [...screens].sort((left, right) => { const leftValue = left[sort as keyof typeof left]; const rightValue = right[sort as keyof typeof right]; return String(rightValue).localeCompare(String(leftValue)) || left.id.localeCompare(right.id); });
  return json({ data: ordered.slice(offset, offset + limit), pagination: { limit, offset, total: ordered.length } });
}

async function listInstruments(url: URL, store: ResearchStore): Promise<Response> {
  const datasetId = await store.activeDatasetId();
  if (!datasetId) return error(503, "No active dataset");
  const sort = url.searchParams.get("sort") ?? "symbol";
  if (!(new Set(["symbol", "name", "assetClass"])).has(sort)) return error(400, "Invalid sort");
  const pagination = readPagination(url);
  if (!pagination) return error(400, "Invalid pagination");
  const { limit, offset } = pagination;
  const instruments = await store.listInstruments(datasetId);
  const ordered = [...instruments].sort((left, right) => String(left[sort as keyof typeof left]).localeCompare(String(right[sort as keyof typeof right])) || left.instrumentId.localeCompare(right.instrumentId));
  return json({ data: ordered.slice(offset, offset + limit), pagination: { limit, offset, total: ordered.length } });
}

async function listGlossary(url: URL, store: ResearchStore): Promise<Response> {
  const query = url.searchParams.get("q")?.trim() ?? "";
  if (query.length > 100) return error(400, "Search query is too long");
  const pagination = readPagination(url);
  if (!pagination) return error(400, "Invalid pagination");
  const normalized = query.toLocaleLowerCase("en-IN");
  const entries = (await store.listGlossary()).filter((entry) => !normalized || [entry.slug, entry.term, ...entry.aliases, entry.summary].some((field) => field.toLocaleLowerCase("en-IN").includes(normalized)));
  const { limit, offset } = pagination;
  return json({ data: entries.slice(offset, offset + limit), pagination: { limit, offset, total: entries.length } });
}

async function getGlossary(slug: string, store: ResearchStore): Promise<Response> {
  const entry = await store.glossary(slug);
  return entry ? json(entry) : error(404, "Glossary entry not found");
}

async function readScreenPayload(request: Request, env: ApiEnv): Promise<{ name: string; source: string } | Response> {
  if (request.headers.get("Content-Type")?.split(";", 1)[0] !== "application/json") return error(415, "Content-Type must be application/json");
  const contentLength = request.headers.get("Content-Length");
  if (contentLength && Number(contentLength) > env.maxBodyBytes) return error(413, "Request body is too large");
  const text = await request.text();
  if (new TextEncoder().encode(text).byteLength > env.maxBodyBytes) return error(413, "Request body is too large");
  let body: unknown;
  try { body = JSON.parse(text); } catch { return error(400, "Invalid JSON"); }
  if (!isRecord(body) || Object.keys(body).some((key) => key !== "name" && key !== "source") || typeof body.name !== "string" || typeof body.source !== "string" || body.name.trim().length === 0 || body.name.length > 120 || body.source.length > 10_000) return error(400, "Invalid screen payload");
  const parsed = parseQuery(body.source);
  if (!parsed.value || parsed.diagnostics.length > 0) return error(400, "Invalid query", parsed.diagnostics);
  const checked = typecheckQuery(parsed.value, DEFAULT_METRIC_CATALOG);
  if (!checked.valid) return error(400, "Invalid query", checked.diagnostics);
  return { name: body.name.trim(), source: body.source };
}
async function createScreen(request: Request, env: ApiEnv, store: ResearchStore, now: () => Date): Promise<Response> {
  const payload = await readScreenPayload(request, env);
  if (payload instanceof Response) return payload;
  const timestamp = now().toISOString();
  return json(await store.createScreen({ name: payload.name, source: payload.source, languageVersion: "v1", createdAt: timestamp, updatedAt: timestamp }), 201);
}
async function updateScreen(screenId: string, request: Request, env: ApiEnv, store: ResearchStore, now: () => Date): Promise<Response> {
  const existing = await store.getScreen(screenId);
  if (!existing) return error(404, "Screen not found");
  const payload = await readScreenPayload(request, env);
  if (payload instanceof Response) return payload;
  const timestamp = now();
  const previousTimestamp = Date.parse(existing.updatedAt);
  const updatedAt = Number.isFinite(previousTimestamp) && timestamp.getTime() <= previousTimestamp
    ? new Date(previousTimestamp + 1).toISOString()
    : timestamp.toISOString();
  return json(await store.updateScreen({ id: screenId, name: payload.name, source: payload.source, updatedAt }));
}

async function getRuns(screenId: string, store: ResearchStore): Promise<Response> {
  const screen = await store.getScreen(screenId);
  if (!screen) return error(404, "Screen not found");
  return json({ screen, runs: await store.listRuns(screenId) });
}
async function getResults(screenId: string, url: URL, store: ResearchStore): Promise<Response> {
  const screen = await store.getScreen(screenId);
  if (!screen) return error(404, "Screen not found");
  const datasetId = await store.activeDatasetId();
  const pagination = readPagination(url);
  if (!pagination) return error(400, "Invalid pagination");
  const sort = url.searchParams.get("sort") ?? "rank";
  if (!(new Set(["rank", "score", "symbol", "assetClass"])).has(sort)) return error(400, "Invalid sort");
  const direction = url.searchParams.get("direction") ?? (sort === "score" ? "desc" : "asc");
  if (direction !== "asc" && direction !== "desc") return error(400, "Invalid sort direction");
  const runs = (await store.listRuns(screenId)).filter((candidate) => candidate.status === "complete");
  const requestedRunId = url.searchParams.get("runId");
  const run = (requestedRunId ? runs.find((candidate) => candidate.id === requestedRunId) : runs[0]) ?? null;
  if (!run) return error(404, "Run not found");
  const enriched = run.matches.filter((match) => !match.exited).map((match) => {
    const explanation = match.explanation;
    const momentum = match.momentum ?? explanation.momentum;
    return { ...match, explanation, ...(momentum ? { momentum } : {}) };
  });
  const ordered = [...enriched].sort((left, right) => {
    const leftValue = sort === "symbol" ? left.symbol ?? "" : sort === "assetClass" ? left.assetClass ?? "" : left[sort as "rank" | "score"] ?? -Infinity;
    const rightValue = sort === "symbol" ? right.symbol ?? "" : sort === "assetClass" ? right.assetClass ?? "" : right[sort as "rank" | "score"] ?? -Infinity;
    const comparison = typeof leftValue === "string" ? leftValue.localeCompare(String(rightValue)) : Number(leftValue) - Number(rightValue);
    return (direction === "asc" ? comparison : -comparison) || left.instrumentId.localeCompare(right.instrumentId);
  });
  const { limit, offset } = pagination;
  return json({ screen, run: { ...run, isCurrentDataset: run.datasetId === datasetId, isCurrentQuery: run.source !== null && run.source !== undefined && run.source === screen.source && run.languageVersion === screen.languageVersion, matches: ordered.slice(offset, offset + limit) }, pagination: { limit, offset, total: ordered.length } });
}
async function getScreen(screenId: string, store: ResearchStore): Promise<Response> {
  const screen = await store.getScreen(screenId);
  return screen ? json(screen) : error(404, "Screen not found");
}
async function runScreen(screenId: string, store: ResearchStore, now: () => Date): Promise<Response> {
  const datasetId = await store.activeDatasetId();
  if (!datasetId) return error(503, "No active dataset");
  const screen = await store.getScreen(screenId);
  if (!screen) return error(404, "Screen not found");
  const completedAt = now().toISOString();
  const status = await store.status(datasetId);
  if (!status.effectiveDate) return error(503, "Dataset effective date is unavailable");
  return json(await store.runScreen(datasetId, screen, status.effectiveDate, completedAt), 201);
}
async function getInstrument(instrumentId: string, store: ResearchStore): Promise<Response> {
  const datasetId = await store.activeDatasetId();
  if (!datasetId) return error(503, "No active dataset");
  const instrument = await store.instrument(datasetId, instrumentId);
  return instrument ? json(instrument) : error(404, "Instrument not found");
}
async function getChart(instrumentId: string, store: ResearchStore): Promise<Response> {
  const datasetId = await store.activeDatasetId();
  if (!datasetId) return error(503, "No active dataset");
  const chart = await store.chart(datasetId, instrumentId);
  if (!chart) return error(404, "Chart not found");
  const headers = new Headers({ "Cache-Control": "private, max-age=300", "Content-Type": chart.contentType ?? "application/json" });
  if (chart.contentEncoding) headers.set("Content-Encoding", chart.contentEncoding);
  return new Response(chart.body as ConstructorParameters<typeof Response>[0], { headers });
}
function isRecord(value: unknown): value is Record<string, unknown> { return typeof value === "object" && value !== null && !Array.isArray(value); }
function readPagination(url: URL): { readonly limit: number; readonly offset: number } | null {
  const limitValue = url.searchParams.get("limit"); const offsetValue = url.searchParams.get("offset");
  if ((limitValue !== null && !/^\d+$/.test(limitValue)) || (offsetValue !== null && !/^\d+$/.test(offsetValue))) return null;
  const limit = limitValue === null ? 50 : Number(limitValue); const offset = offsetValue === null ? 0 : Number(offsetValue);
  return Number.isSafeInteger(limit) && Number.isSafeInteger(offset) && limit > 0 && limit <= 100 && offset >= 0 ? { limit, offset } : null;
}
function methodNotAllowed(allow: string): Response { return new Response(JSON.stringify({ error: { code: "METHOD_NOT_ALLOWED", message: "Method not allowed" } }), { status: 405, headers: { ...jsonHeaders, Allow: allow } }); }
function allowedMethodsFor(path: string): readonly ("GET" | "POST" | "PUT")[] | null {
  if (["/api/v1/status", "/api/v1/metrics", "/api/v1/instruments", "/api/v1/glossary"].includes(path)) return ["GET"];
  if (path === "/api/v1/screens") return ["GET", "POST"];
  if (/^\/api\/v1\/screens\/[^/]+$/.test(path)) return ["GET", "PUT"];
  if (/^\/api\/v1\/screens\/[^/]+\/runs$/.test(path)) return ["GET", "POST"];
  if (/^\/api\/v1\/screens\/[^/]+\/results$/.test(path)) return ["GET"];
  if (/^\/api\/v1\/instruments\/[^/]+(?:\/chart)?$/.test(path)) return ["GET"];
  if (/^\/api\/v1\/glossary\/[^/]+$/.test(path)) return ["GET"];
  return null;
}
