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

export function createApi(dependencies: ApiDependencies): Api {
  const { env, store } = dependencies;
  const now = dependencies.now ?? (() => new Date());
  return { fetch: async (request) => handle(request, env, store, now, dependencies.accessVerifier ?? verifyAccessRequest), issueTestToken: (options) => { if (!dependencies.testAccessSecret) throw new Error("Test token issuance requires an injected test secret"); return issueTestAccessToken(env, dependencies.testAccessSecret, options); } };
}

export type WorkerBindings = ApiEnv & { DB?: unknown; CHARTS?: { get(key: string): Promise<{ body: ReadableStream<Uint8Array>; httpMetadata?: { contentType?: string; contentEncoding?: string } } | null> } };
export function createWorker(env: WorkerBindings): Api {
  const store = env.DB && typeof (env.DB as { prepare?: unknown }).prepare === "function" ? new D1ResearchStore(env.DB as never, env.CHARTS ? { get: async (key) => { const object = await env.CHARTS!.get(key); if (!object) return null; return { body: object.body, ...(object.httpMetadata?.contentType ? { contentType: object.httpMetadata.contentType } : {}), ...(object.httpMetadata?.contentEncoding ? { contentEncoding: object.httpMetadata.contentEncoding } : {}) }; } } : undefined) : new MemoryResearchStore(env.activeDatasetId ?? "dataset-1");
  return createApi({ env, store });
}

export default { fetch(request: Request, bindings: Record<string, unknown>): Promise<Response> { return createWorker({ ...readApiEnv(bindings), ...bindings } as WorkerBindings).fetch(request); } };

async function handle(request: Request, env: ApiEnv, store: ResearchStore, now: () => Date, accessVerifier: AccessVerifier): Promise<Response> {
  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: { Allow: "GET, POST, OPTIONS" } });
  const origin = request.headers.get("Origin");
  if (origin && origin !== env.allowedOrigin) return error(403, "Origin is not allowed");
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/$/, "");
  try { await accessVerifier(request, env); } catch (cause) { return cause instanceof AccessError ? error(cause.status, cause.message) : error(401, "Unauthenticated"); }
  const allowedMethods = allowedMethodsFor(path);
  if (!allowedMethods) return error(404, "Not found");
  if (!allowedMethods.includes(request.method as "GET" | "POST")) return methodNotAllowed(`${allowedMethods.join(", ")}, OPTIONS`);
  if (request.method === "POST" && origin !== env.allowedOrigin) return error(403, "Origin is required for mutations");
  try {
    if (path === "/api/v1/status" && request.method === "GET") return json(await store.status(await store.activeDatasetId()));
    if (path === "/api/v1/metrics" && request.method === "GET") return json({ metrics: await store.metrics() });
    if (path === "/api/v1/instruments" && request.method === "GET") return listInstruments(url, store);
    if (path === "/api/v1/screens" && request.method === "GET") return listScreens(url, store);
    if (path === "/api/v1/screens" && request.method === "POST") return createScreen(request, env, store, now);
    const screenDetail = path.match(/^\/api\/v1\/screens\/([^/]+)$/);
    if (screenDetail && request.method === "GET") return getScreen(decodeURIComponent(screenDetail[1]!), store);
    const screenRuns = path.match(/^\/api\/v1\/screens\/([^/]+)\/runs$/);
    if (screenRuns && request.method === "GET") return getRuns(decodeURIComponent(screenRuns[1]!), store);
    if (screenRuns && request.method === "POST") return runScreen(decodeURIComponent(screenRuns[1]!), store, now);
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

async function createScreen(request: Request, env: ApiEnv, store: ResearchStore, now: () => Date): Promise<Response> {
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
  const timestamp = now().toISOString();
  return json(await store.createScreen({ name: body.name.trim(), source: body.source, languageVersion: "v1", createdAt: timestamp, updatedAt: timestamp }), 201);
}

async function getRuns(screenId: string, store: ResearchStore): Promise<Response> {
  const screen = await store.getScreen(screenId);
  if (!screen) return error(404, "Screen not found");
  return json({ screen, runs: await store.listRuns(screenId) });
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
  return json(await store.runScreen(datasetId, screen, now().toISOString().slice(0, 10)), 201);
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
function allowedMethodsFor(path: string): readonly ("GET" | "POST")[] | null {
  if (["/api/v1/status", "/api/v1/metrics", "/api/v1/instruments"].includes(path)) return ["GET"];
  if (path === "/api/v1/screens") return ["GET", "POST"];
  if (/^\/api\/v1\/screens\/[^/]+$/.test(path)) return ["GET"];
  if (/^\/api\/v1\/screens\/[^/]+\/runs$/.test(path)) return ["GET", "POST"];
  if (/^\/api\/v1\/instruments\/[^/]+(?:\/chart)?$/.test(path)) return ["GET"];
  return null;
}
