# heyp sidecar services

Two optional containers next to the heyp app, together the HTML → image tool. When they are not running, the tool is
listed with `available: false` and a reason; nothing else changes.

| image | what | talks to | interface |
|---|---|---|---|
| `heyp-renderer` | Playwright 1.63.0 + chromium-headless-shell (sandbox on, seccomp profile): URL or HTML → PNG / JPEG | egress only (`render-sbx`) | Unix socket `/run/renderer/r.sock` on a shared volume — `POST /v1/render`, `GET /healthz` |
| `heyp-egress` | stdlib-only policy proxy: resolve once, check every IP, connect to the vetted IP | renderer + internet | :3128 deny, :3129 control (`GET /healthz`; slots need `X-Egress-Secret`), :3130+ one port per render slot |

App side: `app/clients/renderer.py` (`HEYP_RENDERER_SOCKET`) and the tool in `app/tools/render.py`.
Compose definitions: `compose.services.yml` (hardening, networks, limits, healthchecks), which `docker-compose.yml`
and `deploy/compose.prod.yml` follow.

## Build and test

```sh
# each image has a `test` stage (its own suite, same engines and limits) and a `runtime` stage (default)
docker build --target test -t heyp-egress:test services/egress
docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL heyp-egress:test   # 121 tests
docker build --target test -t heyp-renderer:test services/renderer
docker run --rm --network none --read-only --tmpfs /tmp:size=512m,mode=1777 --cap-drop ALL heyp-renderer:test  # 70
docker build -t heyp-renderer:dev services/renderer && docker build -t heyp-egress:dev services/egress

# SSRF / isolation test bed: renderer + egress + hostile targets (internal service, cloud metadata v4/v6,
# DNS rebinding, UDP sink); enforce, Chromium-sandbox-on, proxy-only, raw-proxy and positive-control modes
services/e2e/run.sh

# app-side tests against a real renderer (pytest marker `services`; they skip without this variable)
HEYP_RENDERER_TEST_SOCKET=/run/renderer/r.sock pytest -m services tests/test_clients_renderer.py
```

Python dependencies are hash-locked (`requirements*.lock`, regenerated with `services/lock.sh`, which never resolves
anything published in the last 14 days). Playwright moves to 1.64.0 (Chromium 156) once it is 14 days old
(2026-10-27): edit `renderer/requirements.txt`, run `services/lock.sh renderer`, rebuild, run the e2e suite.

## Production notes

- `HEYP_EGRESS_SECRET` (both renderer and egress) and `HEYP_EGRESS_DENY_CIDRS` (the server's own public IPv4/IPv6)
  are required on a shared VPS; the egress proxy refuses to start without a secret.
- Chromium's sandbox needs `seccomp/chromium-userns.json` (Docker's default profile + `clone`/`unshare`/`chroot`).
  Not yet verified on a host with AppArmor's user-namespace restriction: run the e2e `sandbox` mode on the VPS once;
  if it fails, set `HEYP_RENDER_CHROMIUM_SANDBOX=0` (the network isolation and the egress policy still hold — the
  `proxy-only` mode proves it).
- `gateway_mode_ipv4: isolated` needs Docker Engine ≥ 28; older engines: `com.docker.network.bridge.inhibit_ipv4`.
- Logs: the renderer logs no file names or URLs; egress logs slot, port, verdict, reason class, bytes and duration,
  never the destination host or IP (`HEYP_EGRESS_LOG_HOSTS=1` adds them, for test beds only).
- The renderer image keeps `/usr/share/doc/<package>/copyright` for every Debian package (the licences CREDITS.md
  points to); `tests/test_image.py` fails if a cleanup step deletes them again.
