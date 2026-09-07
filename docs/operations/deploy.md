# Deployment: architecture, gate order, and Cloudflare setup

This describes `.github/workflows/deploy.yml`, `apps/api/wrangler.jsonc`, and
the manual, one-time Cloudflare/GitHub configuration a real deploy needs.

**This document was written without a live Cloudflare account, API token, or
credentials in the authoring environment.** Every command shown below that
touches a real Cloudflare account (`wrangler deploy`, `wrangler d1 migrations
apply --remote`, the CI smoke-test curls) was validated only as far as
`wrangler ... --dry-run` and local static analysis go; none of them were run
against a real account. See "What was actually verified here" at the bottom.

## Architecture: one Worker serves both the API and the SPA

`apps/api/wrangler.jsonc` configures a single Cloudflare Worker per
environment that serves **both**:

- the private JSON API (`apps/api/src/index.ts`, routes under `/api/*`), and
- the built React SPA (`apps/web/dist`, everything else), via [Workers
  Static Assets](https://developers.cloudflare.com/workers/static-assets/).

```jsonc
"assets": {
  "directory": "../web/dist",
  "binding": "ASSETS",
  "not_found_handling": "single-page-application",
  "run_worker_first": ["/api/*"]
}
```

`run_worker_first: ["/api/*"]` means the Worker's own code only runs for
`/api/*`; every other request (the SPA shell and its hashed JS/CSS bundles)
is served directly from the assets binding without invoking
`apps/api/src/index.ts` at all. `not_found_handling: "single-page-application"`
makes client-side routes like `/screens/:id` resolve to `index.html` instead
of 404ing on a hard refresh.

This was a deliberate choice over a separate Cloudflare Pages project for
the SPA, for three reasons:

1. **The application code already assumes same-origin.** `apps/web/src/api.ts`'s
   `createApiClient()` defaults to an empty `baseUrl` (`app.tsx:35`), i.e. it
   was written to call the API at the same origin the page was served from.
   A separate Pages project would need a cross-origin `baseUrl`, CORS
   handling, and a second Access application whose session needs to be
   trusted by the first -- none of which exists in the code today, and none
   of which the brief asked for.
2. **One Access application, not two.** Cloudflare Access enforces at the
   hostname/application level, in front of both a Worker's own `fetch()`
   code and its static-assets binding -- not inside this repository's
   `verifyAccessRequest()` function. One Worker means one hostname means one
   Access application per environment gating *everything* (API and SPA
   alike), which is simpler to reason about and to audit than keeping two
   Access applications (Pages + Worker) in sync.
3. **Cloudflare Pages' own environment model doesn't fit a fully isolated
   preview.** Pages' `env.production` in a Pages-flavoured `wrangler.jsonc`
   only distinguishes the production branch from preview branches *within
   the same project* -- it does not give preview and production distinct
   projects, hostnames, or Access applications the way Workers' `env.preview`
   / `env.production` blocks do (confirmed against Cloudflare's own docs
   while authoring this). Task 14 explicitly wants a preview that "can never
   read or write production data" and a preview credential that "can never
   authenticate against production" -- Workers environments give that
   directly; Pages' branch-scoped model does not.

`apps/web/public/_headers` sets the CSP/HSTS/frame/referrer headers for the
*static-asset* responses (the SPA shell and its bundles); Workers Static
Assets reads `_headers` from the asset directory the same way Cloudflare
Pages does. `apps/api/src/index.ts`'s `SECURITY_HEADERS` constant sets the
same headers for *API* responses, since `_headers` has no effect on
responses a Worker's own code generates. See security-checklist.md for the
exact header values and why each one was chosen.

## Environments: preview and production

`apps/api/wrangler.jsonc` defines `env.preview` and `env.production`, each
with its own:

- Worker name (`stonks-research-api-preview` / `stonks-research-api`)
- D1 database (`stonks-research-preview` / `stonks-research`)
- R2 bucket (`stonks-private-history-preview` / `stonks-private-history`)
- `ACCESS_TEAM_DOMAIN` / `ACCESS_AUD` vars, pointing at two distinct
  Cloudflare Access applications (one per environment)
- `ALLOWED_ORIGIN` var, matching that environment's own hostname

Wrangler does **not** inherit bindings or `vars` from the top-level config
into a named environment (confirmed against Cloudflare's docs while
authoring this) -- each environment block below repeats every binding in
full rather than partially overriding the top-level block. The top-level
(no `--env`) config exists only for an operator's own unqualified
`wrangler dev` and mirrors production's shape; every real deploy or
migration always passes `--env preview` or `--env production` explicitly.

`ACCESS_ALLOWED_EMAILS` is **not** in this file at all -- it is a Cloudflare
Worker secret (`wrangler secret put ACCESS_ALLOWED_EMAILS --env <env>`), set
once per environment out of band. See security-checklist.md.

D1 migrations for both environments read from the same `db/migrations`
directory (`"migrations_dir": "../../db/migrations"` on each `d1_databases`
entry) -- there is exactly one migration history, applied to two databases.

## The gate: `.github/workflows/deploy.yml`

Two jobs, in this exact order:

```text
push to main / workflow_dispatch
        |
   job: preview  (environment: preview)
        |  1. validate source policy   (pytest pipeline/tests/sources/test_registry.py)
        |  2. run all tests            (ruff, mypy, pytest, pnpm lint/typecheck/test, web e2e)
        |  3. build the SPA            (pnpm --filter @stonks/web build -> apps/web/dist)
        |  4. migrate preview          (wrangler d1 migrations apply ... --env preview --remote)
        |  5. deploy preview           (wrangler deploy --env preview)
        |  6. authenticated smoke test (curl with a Cloudflare Access service token)
        v
   job: production  (environment: production, needs: preview)
           7. migrate production      (wrangler d1 migrations apply ... --env production --remote)
           8. deploy production       (wrangler deploy --env production)
           9. read-only production smoke test (GET only, same service-token pattern)
```

This is the exact order the plan's Task 14 brief specifies: "validate
source policy, run all tests, migrate preview, deploy preview,
authenticated smoke test, migrate production, deploy production, read-only
production smoke test." Splitting it into two jobs (rather than nine steps
in one job) is what makes the preview-to-production boundary a **real
gate**, not just a comment: GitHub Actions will not start the `production`
job until the `preview` job's every step has succeeded, and --- once you
configure it (see below) --- will pause for a human approval in between.

### Manual, one-time setup this workflow depends on

None of the following can be expressed in the workflow YAML itself; they
are Cloudflare/GitHub configuration an operator does once, out of band, with
real credentials this environment does not have:

1. **Two Cloudflare Access applications** (one for the preview hostname, one
   for the production hostname), each allowing exactly the owner's identity.
   Record their audience tags as `ACCESS_AUD` in `apps/api/wrangler.jsonc`.
2. **Two Cloudflare Access Service Tokens** (Zero Trust > Access > Service
   Auth), one bound to each Access application, for `deploy.yml`'s smoke
   tests. Store their Client ID/Secret as `CF_ACCESS_SERVICE_TOKEN_ID` /
   `CF_ACCESS_SERVICE_TOKEN_SECRET` in the corresponding GitHub Environment.
3. **Two scoped Cloudflare API tokens** (see security-checklist.md for the
   exact permission set), stored as `CLOUDFLARE_API_TOKEN` in each GitHub
   Environment, plus `CLOUDFLARE_ACCOUNT_ID` (not secret, but environment
   scoped for clarity).
4. **Two repository/environment variables**, `PREVIEW_URL` and
   `PRODUCTION_URL` (e.g. `https://stonks-research-api-preview.<account>.workers.dev`
   and the eventual custom production domain), used only by the smoke-test
   curls.
5. **A required-reviewer protection rule on the `production` GitHub
   Environment** (repository Settings > Environments > production > "Required
   reviewers"). This is the actual human approval gate between preview and
   production; `needs: preview` alone only enforces ordering and success,
   not a pause for sign-off.
6. **Real `database_id` values** in `apps/api/wrangler.jsonc`, replacing the
   `replace-in-deployment*` placeholders, once the D1 databases exist.
7. **A production custom domain** (a `routes` entry in `env.production`),
   once one is registered -- until then, production is reachable at its
   `*.workers.dev` URL, which is also gated by Access.

### Known caveat: the smoke tests are not a full end-to-end auth check yet

`apps/api/src/middleware/access.ts`'s `verifyAccessRequest()` authorizes a
request only if the Access JWT's `email` claim is in the allowlisted
`ACCESS_ALLOWED_EMAILS`. Cloudflare Access **service token** JWTs (used by
the CI smoke tests, since CI cannot complete an interactive login) carry a
`common_name` claim instead of `email`. Access's edge will accept a valid
service token and forward the request; this application's own code will
then still return 401, because the service token has no allowlisted email.

This is intentional under the current design -- there is no bypass route,
and a service token is not silently treated as the owner. But it means
`deploy.yml`'s smoke-test steps currently treat both `200` and `401` as
"the deploy and Access wiring are healthy" (only a redirect to Access's
login page, a 5xx, or a connection failure fails the step), rather than
asserting a strict `200`. Getting to a strict `200` needs a small, explicit,
narrowly-scoped follow-up: allowlisting the CI service token's `common_name`
alongside the owner's `email` in `verifyAccessRequest()`. That is a real
code change to Task 9's access-control module and was deliberately **not**
made as part of this task, per this task's brief ("document that this step
needs a Cloudflare Access Service Token secret ... without inventing
implementation you can't verify"). See `docs/operations/launch-checklist.md`
for how this is tracked as a manual pre-launch verification until that
follow-up lands.

## What was actually verified here (no live Cloudflare account)

- `apps/api/wrangler.jsonc` was validated with `wrangler deploy --dry-run
  --outdir <tmp> --env preview` and `--env production` (bundles the Worker,
  resolves the assets directory, and prints the binding table -- all
  entirely local, no network call to Cloudflare's API). Both environments
  produced the expected distinct D1/R2/var bindings.
- `.github/workflows/deploy.yml` was validated with
  `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/deploy.yml'))"`.
- The security-headers middleware and the anonymous-401 behaviour it wraps
  are covered by real automated tests (see security-checklist.md and
  launch-checklist.md) -- but always via a local in-process or loopback-HTTP
  server, never a deployed Cloudflare Worker.
- `wrangler deploy`, `wrangler d1 migrations apply --remote`, and the
  smoke-test curls were **not executed** -- they require a real
  `CLOUDFLARE_API_TOKEN`/account and are not something this environment can
  or should attempt.
