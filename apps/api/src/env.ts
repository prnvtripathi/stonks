export interface ApiEnv {
  readonly accessIssuer?: string;
  readonly accessTeamDomain?: string;
  readonly accessAudience: string;
  readonly allowedEmails: readonly string[];
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
  return {
    accessTeamDomain: required("ACCESS_TEAM_DOMAIN").replace(/\/$/, ""),
    accessAudience: required("ACCESS_AUD"),
    allowedEmails: emails,
    allowedOrigin: required("ALLOWED_ORIGIN"),
    maxBodyBytes: typeof bindings.MAX_BODY_BYTES === "number" ? bindings.MAX_BODY_BYTES : 16_384,
    ...(typeof bindings.ACCESS_JWKS_URL === "string" && bindings.ACCESS_JWKS_URL.length > 0 ? { accessJwksUrl: bindings.ACCESS_JWKS_URL } : {}),
  };
}
