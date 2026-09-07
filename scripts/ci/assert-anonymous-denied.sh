#!/usr/bin/env bash
# Fail-open detector for .github/workflows/deploy.yml's smoke-test steps.
#
# Sends a plain, unauthenticated GET request (no CF-Access-Client-Id/Secret,
# no Authorization header -- exactly what a real anonymous internet client
# sends) to $1 and FAILS (exit 1) if it gets back HTTP 200.
#
# Why this exists: the existing "authenticated" smoke test in deploy.yml
# treats both 200 and 401 as "healthy," because a Cloudflare Access service
# token's JWT carries a `common_name` claim this application does not yet
# allowlist (see docs/operations/deploy.md's "Known caveat" section) -- so a
# strict 200 assertion there cannot ship yet without a code change to
# apps/api/src/middleware/access.ts. That leaves a real gap: if Access
# itself were ever misconfigured to allow-all, or the accessVerifier() call
# were accidentally removed from apps/api/src/index.ts, an anonymous request
# would get 200 -- and the existing smoke test, which also accepts 200,
# would never notice.
#
# This script closes that specific gap without touching any Access
# verification code and without needing any credential beyond the deployed
# URL itself: a genuinely anonymous request must NEVER succeed with 200.
# 401 (Access allowed the edge request through -- fine, the caller has no
# real identity -- and this application's own check rejected it) or a
# redirect to Access's hosted login page are both "healthy." Only a bare
# 200 is a hard failure, because that specifically means Access (or this
# application's own check) let an unauthenticated request see the response.
set -euo pipefail

url="${1:?usage: assert-anonymous-denied.sh <url>}"

status=$(curl -s -o /dev/null -w '%{http_code}' "$url")
echo "anonymous GET $url -> $status"

if [ "$status" = "200" ]; then
  echo "::error::Anonymous, unauthenticated request to $url returned 200 - Access verification appears to be bypassed (fail-open). Expected 401 (or a redirect to Access's login page), never 200."
  exit 1
fi

exit 0
