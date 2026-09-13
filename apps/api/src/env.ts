export interface ApiEnv {
  readonly accessIssuer?: string;
  readonly accessTeamDomain?: string;
  readonly accessAudience: string;
  readonly allowedEmails: readonly string[];
  /**
   * Exact allowlist of Cloudflare Access service-token `common_name` claims,
   * separate from `allowedEmails`. Deliberately optional: most environments
   * (and every test env literal that predates R12) have no service
   * principal configured at all, in which case any service-token identity
   * is rejected outright by `verifyAccessRequest` rather than falling back
   * to an open allow. See apps/api/src/middleware/access.ts.
   */
  readonly allowedServiceTokenNames?: readonly string[];
  readonly allowedOrigin: string;
  readonly maxBodyBytes: number;
  readonly accessJwksUrl?: string;
  readonly activeDatasetId?: string;
}

export function readApiEnv(bindings: Record<string, unknown>): ApiEnv {
  const required = (name: string): string => {
    const value = bindings[name];
    if (typeof value !== "string" || value.length === 0) throw new Error(`Missing API binding ${name}`);
    return value;
  };
  const emails = required("ACCESS_ALLOWED_EMAILS").split(",").map((email) => email.trim().toLocaleLowerCase("en-US")).filter(Boolean);
  if (emails.length === 0) throw new Error("ACCESS_ALLOWED_EMAILS must contain an email");
  const serviceTokenNames = typeof bindings.ACCESS_ALLOWED_SERVICE_TOKENS === "string"
    ? bindings.ACCESS_ALLOWED_SERVICE_TOKENS.split(",").map((name) => name.trim()).filter(Boolean)
    : [];
  return {
    accessTeamDomain: required("ACCESS_TEAM_DOMAIN").replace(/\/$/, ""),
    accessAudience: required("ACCESS_AUD"),
    allowedEmails: emails,
    allowedOrigin: required("ALLOWED_ORIGIN"),
    maxBodyBytes: typeof bindings.MAX_BODY_BYTES === "number" ? bindings.MAX_BODY_BYTES : 16_384,
    ...(typeof bindings.ACCESS_JWKS_URL === "string" && bindings.ACCESS_JWKS_URL.length > 0 ? { accessJwksUrl: bindings.ACCESS_JWKS_URL } : {}),
    ...(serviceTokenNames.length > 0 ? { allowedServiceTokenNames: serviceTokenNames } : {}),
  };
}
