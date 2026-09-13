#!/usr/bin/env bash
# Fail-open detector for .github/workflows/deploy.yml's smoke-test steps.
#
# Sends a plain, unauthenticated GET request (no CF-Access-Client-Id/Secret,
# no Authorization header -- exactly what a real anonymous internet client
# sends) to $1 and requires genuine evidence of denial: exit 0 only for a
# 401, a 403, or an HTTP redirect to Cloudflare Access's own hosted login
# page. Every other outcome -- a bare 200, a 404, a 5xx, some other
# redirect, or a transport-level failure (timeout, connection refused, DNS
# failure) -- exits 1, because none of those are proof the request was
# actually denied for the right reason.
#
# R12/F15 fix: the previous version of this script failed ONLY on a literal
# 200 and treated everything else -- including a 500, a 404, or a hung
# connection -- as "denied." That is not proof of privacy: a broken
# deployment (misrouted DNS, a crashed Worker, a dropped connection) would
# report "healthy" here just as readily as a correctly enforced Access
# policy would. This version distinguishes genuine denial evidence (401,
# 403, or a redirect whose Location clearly points at Access's hosted login)
# from an inconclusive or failed probe, and only the former passes.
#
# Usage: assert-anonymous-denied.sh <url> [max-time-seconds]
# `max-time-seconds` (default 15) bounds how long a hung/unresponsive
# target is given before this script treats it as a transport failure; it
# exists mainly so this script's own tests (apps/api/e2e/
# assert-anonymous-denied.test.ts) can exercise the timeout path quickly.
set -uo pipefail

url="${1:?usage: assert-anonymous-denied.sh <url> [max-time-seconds]}"
max_time="${2:-15}"

headers_file=$(mktemp)
body_file=$(mktemp)
trap 'rm -f "$headers_file" "$body_file"' EXIT

if ! status=$(curl -s -o "$body_file" -D "$headers_file" -w '%{http_code}' --max-time "$max_time" "$url"); then
  echo "anonymous GET $url -> transport failure (timeout, connection refused, or DNS failure)"
  echo "::error::Anonymous probe of $url failed at the transport layer. This is NOT proof of denial -- it means the probe was inconclusive (the target may simply be unreachable or broken), not that Access correctly rejected the request."
  exit 1
fi

location=$(grep -i '^location:' "$headers_file" 2>/dev/null | tail -n1 | sed -E 's/^[Ll]ocation:[[:space:]]*//' | tr -d '\r\n')

is_access_login_redirect() {
  case "$status" in
    3[0-9][0-9]) ;;
    *) return 1 ;;
  esac
  case "$location" in
    *cloudflareaccess.com*|*/cdn-cgi/access/login*) return 0 ;;
    *) return 1 ;;
  esac
}

case "$status" in
  401|403)
    echo "anonymous GET $url -> $status (genuinely denied)"
    exit 0
    ;;
  200)
    echo "anonymous GET $url -> 200"
    echo "::error::Anonymous, unauthenticated request to $url returned 200 - Access verification appears to be bypassed (fail-open). Expected 401, 403, or a redirect to Access's login page, never 200."
    exit 1
    ;;
  3[0-9][0-9])
    if is_access_login_redirect; then
      echo "anonymous GET $url -> $status redirect to Access login ($location) (genuinely denied)"
      exit 0
    fi
    echo "anonymous GET $url -> $status redirect to '$location'"
    echo "::error::Anonymous request to $url redirected ($status) to a location that is not Access's hosted login page. This is inconclusive, not proof of denial."
    exit 1
    ;;
  *)
    echo "anonymous GET $url -> $status"
    echo "::error::Anonymous request to $url returned unexpected status $status (not 401, 403, or an Access-login redirect). This is inconclusive, not proof of denial."
    exit 1
    ;;
esac
