#!/usr/bin/env bash
# Full-stack smoke test of deploy/compose.prod.yml: app + renderer + egress, started from locally built images exactly
# as heyp-deploy starts them on the VPS, then checked from the outside on the loopback port Apache proxies to.
#
#   .github/scripts/stack-smoke.sh [REGISTRY] [TAG]     # default heyp-ci/<image>:test (as built by ci.yml)
#
# Checks: every container healthy; the public routes (UI, API, legal pages, the /pdf/* redirect to heypdf); the
# renderer reported available; the privacy promises the app itself keeps (no cookies, no-store downloads, no app
# access log in hosted mode); network isolation (the renderer cannot reach the app or the internet; only egress has a
# route out); then `pytest -m services` from the dev image (heyp:dev) against the running renderer socket.
# Apache's own part (headers, access-log format) is checked by .github/scripts/validate-deploy.sh.
# Needs: docker with compose >= 2.24, curl, jq; a free host port STACK_APP_PORT (default 8180, as on the server).
# Leaves nothing behind.
set -euo pipefail
cd "$(dirname "$0")/../../deploy"

REGISTRY=${1:-heyp-ci}
TAG=${2:-test}
PROJECT=heypci
DOMAIN=heyp.localhost
SIBLING=https://heypdf.localhost
WORK=$(mktemp -d)
CANARY=canary$RANDOM$RANDOM
APP_PORT=${STACK_APP_PORT:-8180}

cat >"$WORK/ci.env" <<EOF
COMPOSE_PROFILES=render
HEYP_REGISTRY=$REGISTRY
HEYP_TAG=$TAG
HEYP_PORT=$APP_PORT
HEYP_DOMAIN=$DOMAIN
HEYP_SIBLING_URL=$SIBLING
HEYP_OPERATOR_NAME=CI Operator
HEYP_OPERATOR_EMAIL=privacy@example.com
HEYP_EFFECTIVE_DATE=2026-01-01
HEYP_EGRESS_DENY_CIDRS=203.0.113.7/32
HEYP_EGRESS_SECRET=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
EOF

compose() { docker compose -p "$PROJECT" --env-file "$WORK/ci.env" -f compose.prod.yml "$@"; }
cleanup() {
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
fail() {
  echo "::error::stack smoke: $*"
  compose ps --all || true
  compose logs --no-color --tail 80 || true
  exit 1
}
ok() { echo "ok   $*"; }
# GET (or curl args) on the app's loopback port with the public Host; body -> $WORK/body, headers -> $WORK/headers,
# prints the status.
get() {
  local path=$1
  shift
  curl -sS --max-time 60 -H "Host: $DOMAIN" -o "$WORK/body" -D "$WORK/headers" -w '%{http_code}' "$@" \
    "http://127.0.0.1:$APP_PORT$path" || echo 000
}
expect() { [ "$1" = "$2" ] || fail "$3: HTTP $1, expected $2: $(head -c 300 "$WORK/body")"; }
no_cookie() { if grep -qi '^set-cookie:' "$WORK/headers"; then fail "$1 sets a cookie"; fi; }

compose config --quiet || fail "compose config"
compose up -d --wait --wait-timeout 300 || fail "the stack did not become healthy"
ok "up: $(compose ps --format '{{.Service}}={{.Health}}' | tr '\n' ' ')"

# --- public routes ----------------------------------------------------------------------------------------------------
expect "$(get /api/health)" 200 "health"
jq -e '.status == "ok" and .features.renderer == true' "$WORK/body" >/dev/null \
  || fail "health: renderer not available: $(cat "$WORK/body")"
ok "health: renderer available"
expect "$(get /api/config)" 200 "config"
jq -e --arg sibling "$SIBLING" '.mode == "hosted" and .operator.name == "CI Operator" and .sibling_url == $sibling' \
  "$WORK/body" >/dev/null || fail "config: $(cat "$WORK/body")"
expect "$(get /api/tools)" 200 "tools"
jq -e '[.tools[] | select(.available | not)] == []' "$WORK/body" >/dev/null \
  || fail "unavailable tools: $(jq -c '[.tools[] | select(.available | not) | {id, reason}]' "$WORK/body")"
ok "/api/config (hosted, operator, heypdf link) and every tool available"
for page in privacy terms credits; do
  expect "$(get "/$page")" 200 "/$page"
  grep -q 'class="lg-nameplate"' "$WORK/body" || fail "/$page is not the editorial legal page"
  if grep -qE '\{\{|VERIFY' "$WORK/body"; then fail "/$page has unrendered template text"; fi
  no_cookie "/$page"
done
expect "$(get /privacy)" 200 "/privacy"
grep -q "CI Operator" "$WORK/body" || fail "/privacy does not name the operator"
ok "/privacy, /terms, /credits rendered in hosted mode"
expect "$(get /)" 200 "heyp UI"
no_cookie "/"
grep -qi "content-security-policy: default-src 'self'" "$WORK/headers" || fail "no CSP on /"
grep -q 'heyp' "$WORK/body" || fail "/ is not the heyp page"
code=$(get "/pdf/merge?x=1")
location=$(grep -i '^location:' "$WORK/headers" | tr -d '\r' | cut -d' ' -f2-)
if [ "$code" != 308 ] || [ "$location" != "$SIBLING/merge?x=1" ]; then fail "/pdf/merge: HTTP $code -> '$location'"; fi
ok "heyp at /, CSP, no cookies; /pdf/merge -> 308 $location"

# --- legal/privacy.md: what the app keeps --------------------------------------------------------------------------
docker run --rm --entrypoint python "$REGISTRY/heyp:$TAG" -c '
import io, sys
from PIL import Image
buf = io.BytesIO(); Image.new("RGB", (64, 48), (200, 40, 40)).save(buf, "PNG"); sys.stdout.buffer.write(buf.getvalue())' \
  >"$WORK/$CANARY-name.png"
expect "$(get /api/files -F "files=@$WORK/$CANARY-name.png;type=image/png")" 200 "upload"
file_id=$(jq -r '.files[0].id' "$WORK/body")
expect "$(get "/api/files/$file_id/download")" 200 "download"
grep -qi "^content-disposition:.*$CANARY" "$WORK/headers" || fail "download has no Content-Disposition with the file name"
grep -qiE '^cache-control:.*no-store' "$WORK/headers" || fail "hosted mode: download is not Cache-Control: no-store"
get "/api/health?q=$CANARY-query" -A "$CANARY-agent" -e "https://referrer.example/$CANARY-referer" >/dev/null
sleep 2
if compose logs --no-color app | grep -q "$CANARY"; then fail "the app logged request details in hosted mode"; fi
ok "no app access log in hosted mode; downloads are no-store"

# --- network isolation -----------------------------------------------------------------------------------------------
probe='import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), 4).close()
except OSError:
    sys.exit(0)
sys.exit(1)'
unreachable() { compose exec -T "$1" python -c "$probe" "$2" "$3" || fail "$1 can reach $2:$3"; }
unreachable renderer app 8080
unreachable renderer 1.1.1.1 443
unreachable egress app 8080
gateway=$(docker network inspect "${PROJECT}_render-sbx" -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}')
[ -z "$gateway" ] || unreachable renderer "$gateway" 22
ok "renderer: no route to the app, the host or the internet; egress: no route to the app"

# --- service tests (pytest -m services) against the running renderer --------------------------------------------------
if docker image inspect heyp:dev >/dev/null 2>&1; then
  rc=0
  docker run --rm --network none -v "${PROJECT}_render-sock:/run/renderer" -v "$PWD/..:/app" -w /app \
    -e HEYP_RENDERER_TEST_SOCKET=/run/renderer/r.sock heyp:dev pytest -q -p no:cacheprovider -m services || rc=$?
  case $rc in
    0) ok "pytest -m services" ;;
    5) echo "::notice::no tests marked 'services' yet" ;;
    *) fail "pytest -m services failed ($rc)" ;;
  esac
else
  echo "::notice::heyp:dev not built; skipping pytest -m services"
fi
echo "stack smoke: all checks passed"
