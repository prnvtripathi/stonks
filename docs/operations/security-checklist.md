# Security checklist: identity, tokens, headers, and rotation

A pre-launch and ongoing reference for everything the spec's "Security and
Privacy" section requires. Where an item needs a live Cloudflare account to
complete, it is marked **manual** with exact instructions rather than
claimed as done.

## 1. One owner Access identity, per environment

- Two Cloudflare Access applications exist: one gating the preview hostname,
  one gating the production hostname (see deploy.md). Each application's
  policy allows exactly one identity: the owner's email.
- The application allowlists that same email a second time, independently,
  in `ACCESS_ALLOWED_EMAILS` (`apps/api/src/env.ts`,
  `apps/api/src/middleware/access.ts`). This is deliberate defense in depth:
  even a misconfigured Access policy (e.g. accidentally widened to "everyone
  in this Cloudflare account") still cannot authenticate against the
  Worker, because the Worker independently checks the JWT's `email` claim
  against its own allowlist.
- **Manual:** create/verify both Access applications and their single-email
  policies in Cloudflare Zero Trust > Access > Applications.

## 2. Exact Access audience binding

- Each environment's `ACCESS_AUD` in `apps/api/wrangler.jsonc` must be the
  literal audience tag Cloudflare Access issues for that environment's
  Access application (Zero Trust > Access > Applications > *(app)* >
  Overview > Application Audience (AUD) Tag). A mismatched or missing
  audience is exactly what `verifyAccessRequest()` rejects with "Invalid
  Access token" (`apps/api/src/middleware/access.ts:56`), and is covered by
  the automated test `"rejects invalid issuer, audience, signature, expiry,
  and email"` in `apps/api/src/index.test.ts`.
- **Manual:** after creating each Access application, copy its AUD tag into
  the corresponding `env.preview.vars.ACCESS_AUD` /
  `env.production.vars.ACCESS_AUD` in `apps/api/wrangler.jsonc`, replacing
  the `replace-in-deployment-*-access-aud` placeholder.

## 3. Pipeline service token: not applicable

The spec anticipates "GitHub Actions authenticates with a narrowly scoped
service credential that can publish only the required application data."
As implemented through Task 13, `.github/workflows/daily-data.yml` invokes
the `market-pipeline` CLI, which writes directly to its own SQLite/D1
database via `D1Publisher` -- **the pipeline never calls this Worker's HTTP
API**. There is therefore no pipeline-to-API service token to scope here;
the credential that matters for the pipeline's own write path is
`MARKET_PIPELINE_PUBLISH_TOKEN` (already a placeholder in
`daily-data.yml`'s `daily-data-refresh` GitHub Environment, per Task 13,
for a future direct-D1/R2 publish step).

The service tokens this task *does* introduce
(`CF_ACCESS_SERVICE_TOKEN_ID`/`_SECRET`, one pair per environment) exist
solely so `deploy.yml`'s CI smoke tests can pass Cloudflare Access's edge
check without an interactive login. They are distinct from the owner's
interactive Access identity (different Cloudflare Access object entirely: a
Service Token, not a user policy) and, per the caveat in deploy.md, are
**not** currently allowlisted at the application layer -- they can reach
Access's edge but the Worker's own `email`-based check still rejects them,
so a leaked CI service token cannot read or write application data today.

## 4. No bypass routes

- Every `/api/*` route passes through the same `accessVerifier(request,
  env)` call before any routing logic runs (`apps/api/src/index.ts:43`) --
  there is no route registered before that call, and the `OPTIONS`
  preflight path returns only an `Allow` header with an empty body, never
  data.
- `apps/api/src/index.test.ts`'s `"rejects missing Access identity"` and the
  new black-box test `apps/api/e2e/access-denial.test.ts` both confirm this
  for `GET /api/v1/status`; the new test also confirms it for a mutating
  route (`POST /api/v1/screens`).
- Static assets (the SPA shell, `apps/web/dist/*`) have no separate
  application-layer check -- they rely entirely on Cloudflare Access
  enforcing at the hostname level in front of the Workers Static Assets
  binding (see deploy.md's architecture section). **Manual, pre-launch:**
  after the first real deploy, confirm an anonymous browser request to the
  production hostname's `/` is intercepted by Access's login page rather
  than served the SPA shell -- this cannot be verified without a live
  Cloudflare Access application.
- R2 objects (`stonks-private-history` / `-preview`) are bound to the
  Worker only through the `CHARTS` binding and are never given a public
  bucket policy or a `r2.dev` public URL. **Manual, pre-launch:** confirm in
  the Cloudflare dashboard (R2 > *(bucket)* > Settings) that "Public access"
  remains disabled and no custom domain is attached to either bucket --
  bucket ACLs are an account-console setting with no `wrangler.jsonc`
  representation to lint here.

## 5. Security response headers

Set on every API response by `SECURITY_HEADERS` in `apps/api/src/index.ts`
(applied by the `withSecurityHeaders()` wrapper around every `Api.fetch`
call, so it covers success responses, error responses, and the `OPTIONS`
preflight alike) and, for static assets, by `apps/web/public/_headers`:

| Header | Value | Why |
| --- | --- | --- |
| `Content-Security-Policy` | API: `default-src 'none'; frame-ancestors 'none'`. SPA: `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'` | The API returns only JSON and never HTML, so it allows nothing at all. The SPA has no inline scripts/styles and no third-party embeds (confirmed: `apps/web/index.html` and `apps/web/src/styles.css` reference no external URLs), so every directive is scoped to `'self'` with no `'unsafe-inline'`. |
| `Strict-Transport-Security` | `max-age=63072000; includeSubDomains; preload` | Two-year HSTS with subdomains and preload eligibility, standard for a Cloudflare-fronted origin that is always HTTPS. |
| `X-Frame-Options` | `DENY` | The dashboard is never meant to be framed by anything, including itself. |
| `X-Content-Type-Options` | `nosniff` | Prevents MIME-sniffing a JSON or asset response into something executable. |
| `Referrer-Policy` | `no-referrer` | Saved-screen slugs and instrument IDs in the URL path must never leak to a third-party `Referer` header. |
| `Cache-Control` | Unchanged per route (`private, no-store` for JSON, `private, max-age=300` for chart bodies; `private, no-store` for the SPA shell) | Security headers are additive; caching semantics were already correct (Task 9) and are not touched here. |

Automated coverage: `apps/api/src/index.test.ts`'s `"sets strict security
response headers on every response, without weakening existing cache
directives"`, plus the black-box `apps/api/e2e/access-denial.test.ts`
checking the CSP header over a real HTTP response. The SPA's `_headers` file
is validated by inspection (`docs/operations/deploy.md`'s "What was actually
verified here") and by rebuilding `apps/web/dist` and confirming `_headers`
is copied verbatim by Vite's `public/` directory handling -- Cloudflare's own
enforcement of `_headers` at serve time cannot be verified without a live
deploy (**manual, pre-launch**: `curl -I` the deployed SPA shell and confirm
these headers are present).

## 6. Secret rotation

| Secret | Where it lives | Rotation |
| --- | --- | --- |
| `ACCESS_ALLOWED_EMAILS` | Worker secret, per environment (`wrangler secret put ACCESS_ALLOWED_EMAILS --env <env>`) | Rotate immediately if the owner's email ever changes; otherwise no scheduled rotation (it is an identity, not a credential). |
| `CLOUDFLARE_API_TOKEN` (x2: preview, production) | GitHub Environment secret | Rotate every 90 days, or immediately if `deploy.yml`'s logs are ever suspected of leaking a value (they should not: GitHub Actions redacts registered secrets from logs automatically). Create the replacement token, update the GitHub Environment secret, then revoke the old token in the Cloudflare dashboard -- in that order, so a mid-rotation deploy never has zero valid tokens. |
| `CLOUDFLARE_ACCOUNT_ID` | GitHub Environment (variable, not secret -- it identifies an account, it does not grant access) | No rotation needed; update only if the account changes. |
| `CF_ACCESS_SERVICE_TOKEN_ID` / `_SECRET` (x2) | GitHub Environment secrets | Cloudflare Access service tokens expire on a fixed schedule the operator sets at creation (recommended: 1 year); regenerate before expiry in Zero Trust > Access > Service Auth and update both GitHub Environment secrets together. |
| Access application audience tags | `apps/api/wrangler.jsonc` `vars.ACCESS_AUD` (not secret -- it identifies an application, it does not grant access) | Changes only if an Access application is deleted and recreated. |

## 7. Cloudflare API token scope (documented, not created)

Two tokens, one per environment, each scoped to only that environment's own
resources (Cloudflare API Tokens support per-resource scoping, not just
per-account):

| Permission | Scope | Why |
| --- | --- | --- |
| `Account.Workers Scripts:Edit` | This account, this Worker only if per-script scoping is available in the dashboard's token UI; otherwise the account's Workers Scripts as a whole | `wrangler deploy` |
| `Account.D1:Edit` | Scoped to the one environment's D1 database | `wrangler d1 migrations apply --remote` |
| `Account.R2:Edit` | Scoped to the one environment's R2 bucket | Reserved for future direct-publish tooling; not currently exercised by `deploy.yml`, but the bucket binding is part of what a deploy validates |
| `Account:Read` | This account | Required by Wrangler to resolve the account ID and validate the token itself |

Both tokens additionally need `Zone.DNS:Edit` (or the narrower
custom-domain-route permission the Cloudflare dashboard's token UI offers)
scoped to the zone each environment's `routes` entry names. This is
**required from the first deploy of either environment**, not an
optional/deferred addition: `apps/api/wrangler.jsonc` sets `workers_dev:
false` in both `env.preview` and `env.production`, and Cloudflare Access can
only ever gate a custom-domain route bound to a zone you control -- never a
`*.workers.dev` subdomain, which is not a zone on this account at all. See
deploy.md item 7 for the exact `routes` config each environment needs before
it is reachable.

**Manual:** create both tokens in the Cloudflare dashboard (My Profile > API
Tokens > Create Token > Custom Token) with exactly this permission set, and
store them as each environment's `CLOUDFLARE_API_TOKEN` GitHub secret. Do
not reuse Cloudflare's "Edit Cloudflare Workers" template token as-is -- it
is broader than this list (it typically also grants Workers KV and Workers
Routes edit, which this deployment does not use).

## 8. Supply-chain cooldown: no `minimumReleaseAgeExclude` bypass

`pnpm` enforces a minimum-release-age cooldown on every dependency by
default (currently 24 hours) specifically to defend against a package
compromised and republished the same day CI would otherwise pull it. Task
14's first draft added `apps/api`'s `wrangler` devDependency pinned to
`4.129.1` -- published the same day it was added -- which pulled in five
`workerd` platform binaries and `miniflare` at versions also published that
same day, and `pnpm-workspace.yaml` grew a `minimumReleaseAgeExclude` list
bypassing the cooldown for all eight, undocumented.

**Fix Round 1 resolved this by pinning older versions instead of bypassing
the check:** `apps/api/package.json` now pins `wrangler` to `4.129.0`
(released 2026-09-03, four days before `4.129.1`) rather than the
bleeding-edge patch release. Re-resolving the lockfile against that pin
(`pnpm clean --lockfile && pnpm install`, with zero
`minimumReleaseAgeExclude` entries present) picked up `workerd@1.20260903.1`
and `miniflare@5.20260903.0-alpha` -- both comfortably past the cooldown
window at authoring time -- with no `minimumReleaseAgeExclude` bypass
required anywhere in `pnpm-workspace.yaml`. The full verification suite
(`uv run ruff/mypy/pytest`, `pnpm lint/typecheck/test`,
`pnpm --filter @stonks/web test:e2e`, and `wrangler deploy --dry-run` for
both environments) was re-run against this pin and passed identically to
the `4.129.1` pin -- this is a patch-version downgrade with no functional
difference for anything this repository uses.

If a future dependency bump genuinely needs a feature that only exists in a
same-day release and no older, cooled-down version will do, the correct
process is: (1) verify the exact published artifact against the registry's
own checksum/provenance before excluding it, (2) add a
`minimumReleaseAgeExclude` entry, and (3) document, right here, which
package, why it needed the bypass, what was checked, and a note to
re-evaluate the exclusion at the next dependency bump -- not add the
exclusion silently as a side effect of `pnpm install`.
