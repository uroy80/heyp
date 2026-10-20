#!/usr/bin/env bash
# End-to-end smoke test of the runtime image, run the way docker-compose.yml runs it (read-only rootfs,
# tmpfs /tmp, no-new-privileges, memory limit, uid 10001), on a spare loopback port.
#
#   scripts/smoke.sh                     # build --target runtime as heyp:smoke, then test it
#   scripts/smoke.sh --no-build heyp:next
#   HEYP_SMOKE_PORT=18181 scripts/smoke.sh
#
# Needs only docker and curl on the host (JSON is checked with the container's own Python).
# Exits non-zero on the first failed check; the container is always removed.
set -euo pipefail
cd "$(dirname "$0")/.."

BUILD=1
IMAGE=heyp:smoke
for arg in "$@"; do
  case "$arg" in
    --no-build) BUILD=0 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) IMAGE=$arg ;;
  esac
done
PORT=${HEYP_SMOKE_PORT:-18080}
NAME=heyp-smoke-$$
BASE_URL=http://127.0.0.1:$PORT
WORK=$(mktemp -d)
CODE=

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT
fail() {
  echo "FAIL $*" >&2
  docker logs --tail 40 "$NAME" >&2 2>&1 || true
  exit 1
}
ok() { echo "ok   $*"; }
# req METHOD PATH [curl args...]: response body -> $WORK/body, headers -> $WORK/headers, status -> $CODE
req() {
  local method=$1 path=$2
  shift 2
  CODE=$(curl -sS -o "$WORK/body" -D "$WORK/headers" -w '%{http_code}' -X "$method" "$@" "$BASE_URL$path") || CODE=000
}
expect() { [ "$CODE" = "$1" ] || fail "$2: HTTP $CODE, expected $1: $(head -c 300 "$WORK/body")"; }
header() { grep -i "^$1:" "$WORK/headers" | head -1 | cut -d' ' -f2- | tr -d '\r' || true; }
# json CODE [FILE]: run Python statements against d = the JSON in FILE (default: the last response body)
json() { docker exec -i "$NAME" python -c "import json, sys; d = json.load(sys.stdin); $1" <"${2:-$WORK/body}"; }
process() { req POST /api/process -H 'Content-Type: application/json' -d "$1"; }

# --- build, then read the compose defaults (with no .env / HEYP_* overrides) -----------------------
if [ "$BUILD" = 1 ]; then
  docker build --target runtime -t "$IMAGE" .
fi
env -u HEYP_BIND -u HEYP_PORT -u HEYP_MEM_LIMIT -u HEYP_TMP_SIZE -u HEYP_ALLOW_CROSS_ORIGIN -u HEYP_DOCS -u HEYP_MAX_PIXELS \
  -u HEYP_MAX_JOBS -u HEYP_CACHE_MB -u HEYP_MAX_UPLOAD_MB -u HEYP_MAX_FILES -u HEYP_TTL_MINUTES \
  docker compose --env-file /dev/null -f docker-compose.yml config --format json >"$WORK/compose.json" \
  || fail "docker compose config"
# Prints the docker run flags that mirror the compose service, after asserting its hardened defaults.
RUN_FLAGS=$(docker run --rm -i --entrypoint python "$IMAGE" -c '
import json, sys
s = json.load(sys.stdin)["services"]["heyp"]
p, env = s["ports"][0], s["environment"]
assert p["host_ip"] == "127.0.0.1" and str(p["published"]) == "8080", p
assert env["HEYP_ALLOW_CROSS_ORIGIN"] == "0" and env["HEYP_DOCS"] == "0", env
assert s["read_only"] and s["mem_limit"] and s["pids_limit"], s
assert env["HEYP_MAX_PIXELS"] == "50000000" and env["HEYP_MAX_JOBS"] == "2", env
assert s["tmpfs"] == ["/tmp:size=1g"] and int(s["mem_limit"]) == 4 << 30, s
mem, pids = s["mem_limit"], s["pids_limit"]
flags = ["--read-only", f"--memory={mem}", f"--pids-limit={pids}"]
flags += [f"--tmpfs={t}" for t in s["tmpfs"]] + [f"--security-opt={o}" for o in s["security_opt"]]
flags += [f"--env={k}={v}" for k, v in env.items()]
print(" ".join(flags))' <"$WORK/compose.json") || fail "compose defaults (loopback bind, cross-origin/API docs off, limits)"
ok "compose config: 127.0.0.1:8080, read-only, 4g mem + 1g /tmp, 50 MP / 2 jobs, cross-origin + API docs off"

# --- start like compose -----------------------------------------------------------------------------
# shellcheck disable=SC2086  # RUN_FLAGS is a space-separated flag list without embedded spaces
docker run -d --name "$NAME" -p "127.0.0.1:$PORT:8080" $RUN_FLAGS "$IMAGE" >/dev/null
echo "     image $IMAGE ($(docker image inspect -f '{{.Id}}' "$IMAGE" | cut -c1-19)) on $BASE_URL"
echo "     flags $RUN_FLAGS"

status=starting
for _ in $(seq 1 60); do
  status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$NAME")
  [ "$status" = healthy ] && break
  [ "$(docker inspect -f '{{.State.Running}}' "$NAME")" = true ] || fail "container exited"
  sleep 2
done
[ "$status" = healthy ] || fail "docker health status is '$status' after 120 s"
ok "docker HEALTHCHECK healthy"

# --- container hardening -----------------------------------------------------------------------------
[ "$(docker exec "$NAME" id -u)" = 10001 ] || fail "not running as uid 10001"
docker exec "$NAME" sh -c 'touch /app/x' 2>/dev/null && fail "root filesystem is writable"
dotfiles=$(docker exec "$NAME" find /app /opt/heyp -name '.*')
[ -z "$dotfiles" ] || fail "dotfiles baked into the image: $dotfiles"
ok "uid 10001, read-only rootfs, no dotfiles in the image"

# --- health -----------------------------------------------------------------------------------------
req GET /api/health
expect 200 "health"
# The renderer is a separate service and is off in this single-container run; everything built into the image
# (decoders, the image models) must be on.
json 'f = d["features"]; print("     features", f)
assert d["status"] == "ok" and f and all(v for k, v in f.items() if k != "renderer"), d
assert f["renderer"] is False, d
assert d["limits"]["max_pixels"] == 50_000_000, d' \
  || fail "health features: $(cat "$WORK/body")"
ok "health: status ok, every built-in feature true (renderer off), max_pixels 50 MP"

# --- pages: legal pages, config, tools, the /pdf/* redirect to heypdf ------------------------------------------
req GET /api/config
expect 200 "config"
json 'assert d["mode"] == "local" and d["features"]["renderer"] is False and d["source_url"].startswith("https://"), d
assert d["sibling_url"] == "http://localhost:8081", d' \
  || fail "config: $(cat "$WORK/body")"
for page in /privacy /terms /credits; do
  req GET "$page"
  expect 200 "$page"
  grep -q 'class="lg-nameplate"' "$WORK/body" || fail "$page is not the legal page"
  if grep -qE '\{\{|VERIFY' "$WORK/body"; then fail "$page has unrendered template text"; fi
  if grep -qi '^set-cookie:' "$WORK/headers"; then fail "$page sets a cookie"; fi
done
grep -q "GNU Affero General Public License" "$WORK/body" || fail "/credits does not name the licence"
req GET /
expect 200 "/"
grep -q 'heyp' "$WORK/body" || fail "/ is not the heyp page"
req GET "/pdf/merge?x=1"
expect 308 "/pdf/merge"
[ "$(header location)" = "http://localhost:8081/merge?x=1" ] || fail "/pdf/merge redirects to '$(header location)'"
req GET /api/tools
expect 200 "tools"
json 'ids = {t["id"] for t in d["tools"]}
assert ids == {"img.process", "img.make_gif", "img.compress", "img.html_to_image", "img.upscale", "img.remove_background",
               "img.blur"}, sorted(ids)
off = {t["id"] for t in d["tools"] if not t["available"]}
assert off == {"img.html_to_image"}, off' || fail "tools: $(head -c 400 "$WORK/body")"
ok "/api/config, /privacy, /terms, /credits, /, /pdf/* -> heypdf, /api/tools (only HTML -> image off here)"

# --- jobs: compress a photo (upload → job → result file → download) ---------------------------------------------
docker exec "$NAME" python -c '
import io, sys
import numpy as np
from PIL import Image
x = np.linspace(0, 255, 800, dtype=np.float32)[None, :].repeat(600, 0)
img = Image.fromarray(np.stack([x, x[::-1], np.full_like(x, 96)], -1).astype(np.uint8))
buf = io.BytesIO()
img.save(buf, "PNG")
sys.stdout.buffer.write(buf.getvalue())' >"$WORK/job.png"
req POST /api/files -F "files=@$WORK/job.png;type=image/png"
expect 200 "PNG upload"
JOB_INPUT=$(json 'f = d["files"][0]; assert f["kind"] == "image", f; print(f["id"])') || fail "upload: $(cat "$WORK/body")"
req POST /api/jobs -H 'Content-Type: application/json' \
  -d "{\"tool\":\"img.compress\",\"file_ids\":[\"$JOB_INPUT\"],\"options\":{}}"
expect 202 "compress job"
JOB_ID=$(json 'print(d["job_id"])')
for _ in $(seq 1 60); do
  req GET "/api/jobs/$JOB_ID"
  expect 200 "job status"
  STATUS=$(json 'print(d["status"])')
  case "$STATUS" in done | failed | cancelled) break ;; esac
  sleep 1
done
[ "$STATUS" = "done" ] || fail "compress job ended '$STATUS': $(cat "$WORK/body")"
RESULT_ID=$(json 'f = d["result"]["files"][0]; assert f["kind"] == "image" and f["source"] == "result", f; print(f["id"])') \
  || fail "compress result: $(cat "$WORK/body")"
req GET "/api/files/$RESULT_ID/download"
expect 200 "download"
[ "$(head -c 4 "$WORK/body" | od -An -tx1 | tr -d ' \n')" = 89504e47 ] || fail "download is not a PNG"
header content-disposition | grep -qi 'attachment' || fail "download has no attachment Content-Disposition"
ok "img.compress job: PNG -> smaller PNG, downloaded ($(wc -c <"$WORK/body" | tr -d ' ') bytes)"

# --- HEIC upload (EXIF orientation 6 -> displayed size swapped), convert, resize ---------------------
docker exec "$NAME" python -c '
import io, sys
import numpy as np, pillow_heif
from PIL import Image
pillow_heif.register_heif_opener()
x = np.linspace(0, 255, 1200, dtype=np.float32)[None, :].repeat(900, 0)
img = Image.fromarray(np.stack([x, x[::-1], np.full_like(x, 96)], -1).astype(np.uint8))
exif = Image.Exif()
exif[0x0112] = 6
buf = io.BytesIO()
img.save(buf, "HEIF", quality=80, exif=exif.tobytes())
sys.stdout.buffer.write(buf.getvalue())' >"$WORK/smoke.heic"
req POST /api/files -F "files=@$WORK/smoke.heic;type=image/heic"
expect 200 "HEIC upload"
HEIC_ID=$(json 'f = d["files"][0]; assert (f["format"], f["width"], f["height"]) == ("heic", 900, 1200), f; print(f["id"])') \
  || fail "HEIC upload info: $(cat "$WORK/body")"
ok "upload HEIC: 1200x900 with orientation 6 reads as 900x1200"

process "{\"ids\":[\"$HEIC_ID\"],\"ops\":[],\"output\":{\"format\":\"jpeg\",\"quality\":85}}"
expect 200 "HEIC -> JPEG"
[ "$(header content-type)" = image/jpeg ] || fail "HEIC -> JPEG content-type $(header content-type)"
[ "$(header x-heyp-width)x$(header x-heyp-height)" = 900x1200 ] || fail "HEIC -> JPEG size $(header x-heyp-width)x$(header x-heyp-height)"
[ "$(head -c 3 "$WORK/body" | od -An -tx1 | tr -d ' \n')" = ffd8ff ] || fail "HEIC -> JPEG body is not a JPEG"
ok "process HEIC -> JPEG: image/jpeg 900x1200 ($(wc -c <"$WORK/body" | tr -d ' ') bytes)"

process "{\"ids\":[\"$HEIC_ID\"],\"ops\":[{\"op\":\"resize\",\"mode\":\"long_edge\",\"size\":600}],\"output\":{\"format\":\"png\"}}"
expect 200 "resize"
[ "$(header x-heyp-width)x$(header x-heyp-height)" = 450x600 ] || fail "resize size $(header x-heyp-width)x$(header x-heyp-height)"
ok "process resize long_edge=600 -> PNG 450x600"

# --- pixel guard: a 60 MP PNG (tiny on disk) is refused before it can be decoded --------------------------
docker exec "$NAME" python -c '
import io, sys
from PIL import Image
buf = io.BytesIO()
Image.new("L", (10000, 6000)).save(buf, "PNG")
sys.stdout.buffer.write(buf.getvalue())' >"$WORK/big.png"
req POST /api/files -F "files=@$WORK/big.png;type=image/png"
expect 413 "60 MP upload"
ok "upload 60 MP PNG ($(wc -c <"$WORK/big.png" | tr -d ' ') bytes) -> 413"

# --- models in the hardened runtime: face detection, segmentation, passport ----------------------------
req POST /api/files -F "files=@tests/fixtures/astronaut.png;type=image/png"
expect 200 "PNG upload"
FACE_ID=$(json 'print(d["files"][0]["id"])')
req GET "/api/files/$FACE_ID/faces"
expect 200 "faces"
json 'assert len(d["faces"]) >= 1, d' || fail "no face found in tests/fixtures/astronaut.png"
ok "faces: YuNet finds the face in astronaut.png"

process "{\"ids\":[\"$FACE_ID\"],\"ops\":[{\"op\":\"background\",\"mode\":\"replace\",\"color\":\"#3366ff\"}],\"output\":{\"format\":\"png\"}}"
expect 200 "background replace (U2-Net)"
[ "$(header content-type)" = image/png ] || fail "background content-type $(header content-type)"
ok "background replace (U2-Net) -> image/png"

process "{\"ids\":[\"$FACE_ID\"],\"ops\":[{\"op\":\"passport\",\"preset\":\"us\"},{\"op\":\"sheet\",\"paper\":\"4x6\"}],\"output\":{\"format\":\"jpeg\"}}"
expect 200 "passport + sheet"
warnings=$(header x-heyp-warnings)
case "$warnings" in *[Nn]o%20face*|*[Uu]navailable*) fail "passport warnings: $warnings" ;; esac
ok "passport us + 4x6 sheet -> $(header x-heyp-width)x$(header x-heyp-height) $(header content-type)"

# --- service hardening --------------------------------------------------------------------------------
req POST /api/files -H 'Origin: https://evil.example' -F "files=@$WORK/smoke.heic;type=image/heic"
expect 403 "cross-origin upload"
ok "cross-origin upload -> 403"
req GET /docs
expect 404 "/docs with HEYP_DOCS=0"
req GET /.DS_Store
expect 404 "dotfile"
ok "/docs and /.DS_Store -> 404"

# --- delete ------------------------------------------------------------------------------------------
for id in "$HEIC_ID" "$FACE_ID"; do
  req DELETE "/api/files/$id"
  expect 204 "delete"
  req GET "/api/files/$id"
  expect 404 "get after delete"
done
ok "delete -> 204, then 404"

echo "smoke: all checks passed"
