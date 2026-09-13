import type { ApiEnv } from "../env";

export interface AccessClaims {
  readonly iss: string;
  readonly aud: string | readonly string[];
  /** Owner identity claim. Absent for a scoped service-token identity. */
  readonly email?: string;
  /** Cloudflare Access service-token identity claim. Absent for an owner. */
  readonly commonName?: string;
  readonly exp: number;
  readonly [key: string]: unknown;
}
export class AccessError extends Error { public readonly status: number; public constructor(message = "Unauthenticated", status = 401) { super(message); this.name = "AccessError"; this.status = status; } }
export type AccessVerifier = (request: Request, env: ApiEnv) => Promise<AccessClaims>;
type RsaJwk = { readonly kty: "RSA"; readonly n: string; readonly e: string; readonly kid?: string; readonly alg?: string };
type VerifyKey = Awaited<ReturnType<typeof crypto.subtle.importKey>>;
const keyCache = new Map<string, { readonly expiresAt: number; readonly keys: ReadonlyMap<string, VerifyKey> }>();
const KEY_CACHE_TTL_MS = 5 * 60 * 1000;

function decode(value: string): Uint8Array { const normalized = value.replaceAll("-", "+").replaceAll("_", "/") + "=".repeat((4 - value.length % 4) % 4); return Uint8Array.from(atob(normalized), (character) => character.charCodeAt(0)); }
function decodeJson(value: string): Record<string, unknown> { try { const parsed: unknown = JSON.parse(new TextDecoder().decode(decode(value))); if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error("not an object"); return parsed as Record<string, unknown>; } catch { throw new AccessError("Invalid Access token"); } }
function base64Url(value: ArrayBuffer): string { return btoa(String.fromCharCode(...new Uint8Array(value))).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", ""); }
function issuer(env: ApiEnv): string { const value = env.accessIssuer ?? env.accessTeamDomain; if (!value) throw new AccessError("Access issuer is not configured"); return value.startsWith("http") ? value.replace(/\/$/, "") : `https://${value}`; }
function sameAudience(actual: unknown, expected: string): boolean { return typeof actual === "string" ? actual === expected : Array.isArray(actual) && actual.includes(expected); }
function isRecord(value: unknown): value is Record<string, unknown> { return typeof value === "object" && value !== null && !Array.isArray(value); }
function isJwk(value: unknown): value is RsaJwk { return isRecord(value) && value.kty === "RSA" && typeof value.n === "string" && typeof value.e === "string"; }
function isStatusSmokeCheck(request: Request): boolean {
  if (request.method !== "GET") return false;
  const path = new URL(request.url).pathname.replace(/\/$/, "");
  return path === "/api/v1/status";
}

/**
 * Shared authorization decision for a verified (signature/issuer/audience/
 * expiry already checked) set of claims, used by both the production RS256
 * verifier and the HS256 test double so the two paths cannot drift.
 *
 * Two disjoint identity kinds:
 *  - Owner: an `email` claim matched against `env.allowedEmails`. Authorized
 *    for every route this Worker exposes.
 *  - Service principal: a `common_name` claim (how Cloudflare Access signs
 *    a Service Token JWT) matched against the separate, explicit
 *    `env.allowedServiceTokenNames` allowlist. Authorized ONLY for
 *    `GET /api/v1/status` -- never mapped to an owner email, never
 *    authorized for any mutation or any other read route. An otherwise
 *    valid, allowlisted service token used outside that one route is a 403
 *    (a real, verified identity that is simply not in scope here), while an
 *    unrecognized or unconfigured identity of either kind is a 401.
 */
function authorizeIdentity(claims: Record<string, unknown>, env: ApiEnv, request: Request): { readonly email?: string; readonly commonName?: string } {
  const email = typeof claims.email === "string" ? claims.email.toLocaleLowerCase("en-US") : "";
  if (email) {
    if (!env.allowedEmails.some((allowed) => allowed.toLocaleLowerCase("en-US") === email)) throw new AccessError("Access identity is not allowlisted");
    return { email };
  }
  const commonName = typeof claims.common_name === "string" ? claims.common_name : "";
  const allowedServiceTokenNames = env.allowedServiceTokenNames ?? [];
  if (!commonName || !allowedServiceTokenNames.includes(commonName)) throw new AccessError("Access identity is not allowlisted");
  if (!isStatusSmokeCheck(request)) throw new AccessError("Service identity is not authorized for this route", 403);
  return { commonName };
}

async function loadKeys(url: string, fetcher: typeof fetch, force = false): Promise<ReadonlyMap<string, VerifyKey>> {
  const cached = keyCache.get(url);
  if (!force && cached && cached.expiresAt > Date.now()) return cached.keys;
  let response: Response;
  try { response = await fetcher(url, { method: "GET", headers: { Accept: "application/json" } }); } catch { throw new AccessError("Access key service unavailable"); }
  if (!response.ok) throw new AccessError("Access key service unavailable");
  let body: unknown;
  try { body = await response.json(); } catch { throw new AccessError("Invalid Access key document"); }
  const jwks: readonly RsaJwk[] = Array.isArray(body) ? body.filter(isJwk) : isRecord(body) && Array.isArray(body.keys) ? body.keys.filter(isJwk) : [];
  if (jwks.length === 0) throw new AccessError("Invalid Access key document");
  const imported = new Map<string, VerifyKey>();
  for (const jwk of jwks) {
    if (!jwk.kid || (jwk.alg && jwk.alg !== "RS256")) continue;
    try { imported.set(jwk.kid, await crypto.subtle.importKey("jwk", jwk as never, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"])); } catch { /* ignore malformed keys */ }
  }
  if (imported.size === 0) throw new AccessError("Invalid Access key document");
  keyCache.set(url, { keys: imported, expiresAt: Date.now() + KEY_CACHE_TTL_MS });
  return imported;
}

export async function verifyAccessRequest(request: Request, env: ApiEnv): Promise<AccessClaims> {
  const token = request.headers.get("Cf-Access-Jwt-Assertion");
  if (!token) throw new AccessError();
  const pieces = token.split(".");
  if (pieces.length !== 3) throw new AccessError("Invalid Access token");
  const [encodedHeader, encodedClaims, encodedSignature] = pieces;
  if (!encodedHeader || !encodedClaims || !encodedSignature) throw new AccessError("Invalid Access token");
  const header = decodeJson(encodedHeader);
  if (header.alg !== "RS256" || header.typ !== "JWT" || typeof header.kid !== "string") throw new AccessError("Invalid Access token");
  const claims = decodeJson(encodedClaims);
  const expectedIssuer = issuer(env);
  if (claims.iss !== expectedIssuer || !sameAudience(claims.aud, env.accessAudience)) throw new AccessError("Invalid Access token");
  if (typeof claims.exp !== "number" || !Number.isFinite(claims.exp) || claims.exp <= Math.floor(Date.now() / 1000)) throw new AccessError("Expired Access token");
  const identity = authorizeIdentity(claims, env, request);
  const jwksUrl = env.accessJwksUrl ?? `${expectedIssuer}/cdn-cgi/access/certs`;
  let keys = await loadKeys(jwksUrl, fetch);
  let key = keys.get(header.kid);
  if (!key) { keys = await loadKeys(jwksUrl, fetch, true); key = keys.get(header.kid); }
  if (!key) throw new AccessError("Unknown Access signing key");
  try { if (!await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, decode(encodedSignature), new TextEncoder().encode(`${encodedHeader}.${encodedClaims}`))) throw new AccessError("Invalid Access token"); } catch (cause) { if (cause instanceof AccessError) throw cause; throw new AccessError("Invalid Access token"); }
  return { ...claims, iss: expectedIssuer, aud: claims.aud as string | readonly string[], ...identity, exp: claims.exp };
}

/** Test-only deterministic verifier, injected into createApi and never used by the Worker entrypoint. */
export function createTestAccessVerifier(secret: string): AccessVerifier {
  return async (request, env) => {
    const token = request.headers.get("Authorization")?.match(/^Bearer\s+([^\s]+)$/i)?.[1];
    if (!token) throw new AccessError();
    const pieces = token.split(".");
    if (pieces.length !== 3) throw new AccessError("Invalid Access token");
    const [encodedHeader, encodedClaims, encodedSignature] = pieces;
    if (!encodedHeader || !encodedClaims || !encodedSignature) throw new AccessError("Invalid Access token");
    const header = decodeJson(encodedHeader); const claims = decodeJson(encodedClaims);
    if (header.alg !== "HS256" || claims.iss !== issuer(env) || !sameAudience(claims.aud, env.accessAudience) || typeof claims.exp !== "number" || claims.exp <= Math.floor(Date.now() / 1000)) throw new AccessError("Invalid Access token");
    const identity = authorizeIdentity(claims, env, request);
    const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    const expected = base64Url(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(`${encodedHeader}.${encodedClaims}`)));
    if (expected !== encodedSignature) throw new AccessError("Invalid Access token");
    return { ...claims, iss: issuer(env), aud: claims.aud as string | readonly string[], ...identity, exp: claims.exp };
  };
}

/**
 * Exactly one of `email` (owner identity) or `commonName` (Access
 * service-token identity, carried as the `common_name` claim) must be set --
 * mirrors the real Access token shapes this test double stands in for.
 */
export interface TestTokenOptions { readonly email?: string; readonly commonName?: string; readonly exp?: number; readonly aud?: string; }
export async function issueTestAccessToken(env: ApiEnv, secret: string, options: TestTokenOptions): Promise<string> {
  const encode = (value: unknown): string => base64Url(new TextEncoder().encode(JSON.stringify(value)).buffer);
  const header = encode({ alg: "HS256", typ: "JWT" });
  const claims = encode({
    iss: issuer(env),
    aud: options.aud ?? env.accessAudience,
    ...(options.email !== undefined ? { email: options.email } : {}),
    ...(options.commonName !== undefined ? { common_name: options.commonName } : {}),
    exp: options.exp ?? Math.floor(Date.now() / 1000) + 3600,
  });
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return `${header}.${claims}.${base64Url(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(`${header}.${claims}`)))}`;
}
