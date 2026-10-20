# Deploying heyp to heyp.edgr.in

heyp runs on one Hostinger VPS from images that GitHub Actions builds, attests and publishes to GHCR. Nothing is
built on the server, and nothing outside the `heyp` compose project is touched (`heyp-deploy` prunes only images
labelled as this repository's). Apache httpd on the host already serves other sites on 80/443 and gets one more
site; Cloudflare proxies every name. heypdf, the PDF toolbox, is a separate app from its own repository
(github.com/uroy80/heypdf) and runs next to heyp as its own stack — nothing here deploys or changes it.

```
push to main ─► CI: lint · tests (Python, UI, services) · 3 images built, smoke-tested, scanned · full-stack compose
             └► Release: amd64 + arm64 ─► ghcr.io/uroy80/{heyp,heyp-renderer,heyp-egress}:sha-<commit>
                attest provenance + SBOM ─► Deploy (environment "production")
                                              │ verify attestations · ssh deploy@vps deploy <commit> (forced command)
                                              ▼
VPS: heyp-deploy ─ commit must be on main ─ compose pull ─ up --wait ─ revision labels ─ unhealthy? previous release

visitor ─HTTPS─► Cloudflare ─HTTPS, Full (strict), Origin cert─► Apache :443 (conf.d/zz-heyp-ssl.conf)
                                                                   │ mod_remoteip: CF-Connecting-IP → the visitor
                                                                   │ log: /var/log/httpd-heyp/access.log, 14 days
                                                                   ▼ X-Forwarded-For: <visitor> · Proto: https
                                               127.0.0.1:8180 ─► app ─ Unix socket ─► renderer (Chromium, render-sbx)
                                                                                         └─► egress ─► internet
```

## 1. The server

| | |
|---|---|
| Machine | Hostinger VPS 147.93.18.165 — AlmaLinux 10.1, x86_64, 4 vCPU / 15 GB, Docker 29.5 with Compose v5 (heyp needs Docker Engine ≥ 28 for isolated bridge gateways and Compose ≥ 2.24; `bootstrap-vps.sh` checks both). |
| Web server | **Apache httpd 2.4** on 80/443 for every site, with a Cloudflare Origin certificate for `*.edgr.in`: `/etc/ssl/edgr/origin.pem` + `origin.key` (and Cloudflare's origin CA, `cloudflare-origin-ca.pem`). |
| Ports already taken | 3000, 4416, 8100 (other sites), **8181 (heypdf)**. heyp's app listens on **127.0.0.1:8180**. |
| Memory / CPU | heyp's ceilings (not reservations): app 4 GB (its `/tmp` with the uploads is RAM and counts here), renderer 1 GB, egress 128 MB ≈ 5.1 GB; idle ≈ 0.8 GB. CPU caps: app 2, renderer 1, egress 0.25 — no container can take the whole machine from heypdf and the other sites. |
| Swap | **Off** (or encrypted). Uploads live only in RAM (tmpfs) and `/privacy` says so; swap could write them to disk. Check with `swapon --show`. |
| Disk | ~5 GB for images (three images × the last few releases); `heyp-deploy` keeps 5 releases and prunes older images. |
| DNS | `heyp.edgr.in`, proxied by Cloudflare (§6). |
| GitHub | The public repository `uroy80/heyp` and its `models-v1` release (§7.1). |

## 2. GitHub repository settings (once)

1. **Settings → Actions → General**: workflow permissions "Read repository contents and packages"; "Require
   approval for all external contributors" for fork PRs.
2. **Settings → Code security**: Dependabot alerts + security updates, secret scanning + push protection,
   private vulnerability reporting (SECURITY.md links it). Leave CodeQL *default setup* off (ci.yml runs it).
3. **Settings → Rules → Rulesets** — `main`: require a pull request, the status check **CI OK** and review from
   Code Owners (allow yourself to bypass as the only maintainer), block force pushes and deletion. Tags `v*`: only
   you may create them; block updates and deletion.
4. **Settings → General → Releases**: turn on **release immutability**.
5. **Settings → Environments → `production`** — deployment branches: *Selected* → `main` (and `v*` tags if you
   deploy releases by hand); optional required reviewers (every deploy waits for your approval).

   | Environment secrets | |
   |---|---|
   | `DEPLOY_SSH_KEY` | the private CI key (§3), whole file |
   | `DEPLOY_KNOWN_HOSTS` | the lines `bootstrap-vps.sh` prints (check the fingerprint first) |
   | `PROD_ENV` (optional) | extra `HEYP_*` lines sent with each deploy, e.g. `HEYP_MAX_UPLOAD_MB=80`. Network and identity keys (`HEYP_TAG`, `HEYP_REGISTRY`, `HEYP_BIND`, `HEYP_PORT`, `HEYP_TRUSTED_PROXIES`, `HEYP_EGRESS_DENY_CIDRS`, subnets) are refused: they stay in `/etc/heyp/heyp.env`. |

   | Environment variables | |
   |---|---|
   | `DEPLOY_HOST` | `147.93.18.165` |
   | `DEPLOY_PORT`, `DEPLOY_USER` | only if not `22` / `deploy` |
   | `HEYP_URL` | `https://heyp.edgr.in` — checked after each deploy: `/api/health`, `/`, `/privacy` (and no cookie or `/cdn-cgi/` script on `/`, `/privacy`, `/terms`) |

6. **Repository variables**: `DEPLOY_ENABLED=true` once the first manual deploy worked (§7); optional
   `COSIGN_SIGN=true`; `HEYP_MODELS_MIRROR` only while the `models-v1` assets don't exist yet (§7.1 — create the
   release before the first push to `main`).

Secrets live only in the environment, so only the deploy job can read them; CI, scanners and pull requests never
see the SSH key. No Hostinger API token and no personal access token are needed.

## 3. The deploy key

```bash
ssh-keygen -t ed25519 -N '' -C heyp-ci -f heyp_ci
# heyp_ci      → GitHub secret DEPLOY_SSH_KEY;   heyp_ci.pub → bootstrap-vps.sh --ci-key "$(cat heyp_ci.pub)"
```

On the server the key is pinned to `restrict,command="/usr/local/sbin/heyp-deploy"`: no shell, no port
forwarding, only `deploy <commit>`, `rollback`, `status` and `version` — and `deploy` only accepts commits on
`main` (or tagged `v*`) of the public repository; compose files come from that commit, never from CI. heypdf's CI
key may sit in the same `deploy` user's `authorized_keys` with its own forced command; neither key can run the
other's program. Delete the local `heyp_ci` after storing it.

## 4. Prepare the server (once)

1. hPanel → VPS → **Snapshots & backups**: take a snapshot (Hostinger keeps one per VPS, so this replaces any older
   one).
2. Bootstrap from a checkout:
   ```bash
   git clone https://github.com/uroy80/heyp.git /tmp/heyp
   sudo bash /tmp/heyp/deploy/bootstrap-vps.sh --ci-key "ssh-ed25519 AAAA… heyp-ci" --egress-guard
   ```
   It accepts AlmaLinux (missing tools come from `dnf`), creates the `deploy` user (docker group, password locked),
   `/srv/heyp` (git mirror, releases, state) and `/etc/heyp/heyp.env` from `deploy/heyp.env.example` — with
   **`HEYP_EGRESS_DENY_CIDRS`** set to this server's public IPv4/IPv6 addresses and a fresh `HEYP_EGRESS_SECRET` —
   installs `heyp-deploy`, warns if swap is on, and with `--egress-guard` adds host firewall rules that keep the
   egress proxy's network away from private, loopback and link-local addresses and from this host's own ports
   (defence in depth; the proxy already refuses them). Don't pass `--ufw` (it is for Ubuntu; use hPanel's
   firewall). It prints the `DEPLOY_KNOWN_HOSTS` lines: compare the fingerprint with
   `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` in hPanel's browser terminal before pasting them into GitHub.
3. Check `/etc/heyp/heyp.env` (root:deploy, 0640). It already holds the production values: `HEYP_DOMAIN=heyp.edgr.in`,
   `HEYP_SIBLING_URL=https://heypdf.edgr.in`, `COMPOSE_PROFILES=render`, the app on **127.0.0.1:8180**, the operator
   (**`HEYP_OPERATOR_NAME`** "Usham Roy", **`HEYP_OPERATOR_EMAIL`** support@edgr.in, also the grievance officer —
   hosted mode refuses to start without them, they're published in the privacy policy), jurisdiction India,
   `HEYP_CDN_PROVIDER=Cloudflare` for `/privacy` and `/terms`, uploads up to 90 MB and upload requests up to 95 MiB
   (`HEYP_MAX_REQUEST_MB`; Cloudflare's cap is 100 MB per request) and the sizing of §1. Fill in
   `HEYP_EFFECTIVE_DATE` (the launch date) and `HEYP_SERVER_LOCATION` (hPanel → VPS → Overview). If more than one
   public IP points at the VPS, add each to `HEYP_EGRESS_DENY_CIDRS`.
4. Check that nothing else uses the port or heyp's two fixed Docker subnets (heypdf's stack must use others):
   ```bash
   sudo ss -tlnp 'sport = :8180'                                              # no output
   docker network inspect $(docker network ls -q) --format '{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}}' \
     | grep -E '172\.31\.25[12]\.' || echo free                                 # "free"
   ```
   Another port: change `HEYP_PORT` in `/etc/heyp/heyp.env` **and** both `ProxyPass` lines in `zz-heyp-ssl.conf`.
   Other subnets: `HEYP_EDGE_SUBNET` / `HEYP_EGRESS_SUBNET` in `/etc/heyp/heyp.env` (rerun bootstrap with
   `--egress-guard` afterwards, so the guard matches).
5. **Firewall** — hPanel → VPS → Security → **Firewall** (filters before traffic reaches the VPS; once a rule set is
   active everything else is dropped, so include what the other sites need): accept TCP 22 (or your SSH port), TCP
   80, TCP 443. GitHub-hosted runners have no fixed addresses, so SSH stays open; key-only login plus the forced
   command is the protection. Ports Docker publishes bypass host firewalls, which is why heyp publishes the app on
   127.0.0.1 only.
6. **Trusted proxy.** The app takes the client address (for rate limits) from `X-Forwarded-For` only when the request
   comes from `HEYP_TRUSTED_PROXIES` — never `*`. The default (the `edge` network, `172.31.251.0/24`) is right:
   Apache's connections to the published loopback port arrive from that network's gateway. Cloudflare's addresses
   never go here: Apache resolves the visitor (§5) and sends exactly one `X-Forwarded-For` entry.
7. **Chromium sandbox.** The renderer runs Chromium's own sandbox with `deploy/seccomp/chromium-userns.json`
   (Docker's default profile plus `clone`/`unshare`/`chroot`); AlmaLinux has no AppArmor user-namespace
   restriction. After the first deploy, `docker logs heyp-renderer-1` must not say `No usable sandbox`.

## 5. Apache

```bash
cd /tmp/heyp/deploy/apache
sudo install -d -m 750 /var/log/httpd-heyp
sudo install -m 644 heyp-cloudflare-ips.inc /etc/httpd/conf.d/heyp-cloudflare-ips.inc
sudo install -m 644 ssl-heyp.conf /etc/httpd/conf.d/zz-heyp-ssl.conf
sudo install -m 644 heyp.conf /etc/httpd/conf.d/zz-heyp.conf
sudo install -m 644 logrotate-heyp /etc/logrotate.d/heyp-httpd
sudo install -m 755 refresh-cf-ips.sh /usr/local/sbin/heyp-refresh-cf-ips
echo '17 4 1 * * root /usr/local/sbin/heyp-refresh-cf-ips' | sudo tee /etc/cron.d/heyp-refresh-cf-ips
sudo apachectl -t && sudo systemctl reload httpd
sudo httpd -S     # the default (first) *:80 and *:443 vhosts must still be your existing sites
```

The `zz-` names load heyp after the existing vhosts, so it never becomes the site Apache answers for unknown names.
The files configure `heyp.edgr.in` only (heypdf ships its own vhost) and define no Apache variables, which would be
global to the whole server. What the configuration does, and why each part matters:

| Part | Why |
|---|---|
| `RemoteIPHeader CF-Connecting-IP` + `heyp-cloudflare-ips.inc` (one `RemoteIPTrustedProxy` per Cloudflare range) | Every connection comes from a Cloudflare edge. Without this, the access log records Cloudflare's addresses (`/privacy` promises yours, and "send us your IP address" could not be honoured), and the app's per-visitor limits — uploads per hour, jobs per minute, captures per minute, active jobs — are shared by everyone behind the same few edge addresses. Only Cloudflare's ranges are believed, so nobody can pick their own address by sending the header directly. |
| `RequestHeader unset X-Forwarded-For early`, `ProxyAddHeaders On` | The app takes the right-most `X-Forwarded-For` entry its trusted proxy adds; Apache sends exactly one: the visitor. |
| `ProxyPreserveHost On`, `RequestHeader set X-Forwarded-Proto "https"` | Redirects the app builds keep the public name and `https`; the cross-origin guard sees the real host. |
| Own `CustomLog` (`%a`, `%U`), `ErrorLog`, `ErrorLogFormat`, `LogLevel warn` in **every** heyp vhost | What `/privacy` describes (§10). |
| `logrotate-heyp`, logs in `/var/log/httpd-heyp/` | Deleted after 14 days (`HEYP_LOG_RETENTION_DAYS`); the stock rule for `/var/log/httpd/` keeps ~5 weeks. |
| `LimitRequestBody 104857600` | Cloudflare's 100 MB request cap. `HEYP_MAX_UPLOAD_MB=90` keeps a single file below it and `HEYP_MAX_REQUEST_MB=95` makes the app refuse bigger upload requests and publish the cap in `/api/config`, so the UI splits uploads into requests that pass. |
| `ProxyPass … timeout=600 retry=0`, no compression for `…/events` | Server-sent events (job progress) stream unbuffered; jobs run in the background, so no request waits for a whole job (Cloudflare gives up after 100 s without a first byte). |
| `Strict-Transport-Security` | The app sets CSP, nosniff, Referrer-Policy and Cache-Control; only HSTS is added here. |

`refresh-cf-ips.sh` keeps the range list current (monthly cron above); a failed or odd download, or a list that
breaks `apachectl -t`, leaves the current file in place. SELinux is off on this server; if you enable it, also run
`setsebool -P httpd_can_network_connect 1` and
`semanage fcontext -a -t httpd_log_t '/var/log/httpd-heyp(/.*)?' && restorecon -R /var/log/httpd-heyp`.
Optional: Cloudflare **Authenticated Origin Pulls** plus the commented `SSLVerifyClient` lines in `zz-heyp-ssl.conf`
make the origin refuse connections that bypass Cloudflare. CI runs `httpd -t` on these files and a functional check of the visitor
address and the log format in a throwaway `httpd:2.4` container (`.github/scripts/validate-deploy.sh`).

## 6. Cloudflare (dashboard → edgr.in)

`/privacy` says there are no cookies, no analytics and no scripts from other companies, and that connections are
encrypted all the way to the server. These settings keep that true for `heyp.edgr.in` (use Configuration Rules where
a setting would otherwise change the other sites of the zone):

| Setting | Value | Why |
|---|---|---|
| DNS | `A heyp` → 147.93.18.165 (`AAAA` only if Apache listens on IPv6), **Proxied** | heyp is reached only through Cloudflare. |
| SSL/TLS → encryption mode | **Full (strict)** | "Encrypted again between Cloudflare and our server" (`/privacy`); *Flexible* would be plain HTTP to the origin. |
| Edge Certificates → Always Use HTTPS | On | Port 80 only redirects anyway. HSTS: here **or** in Apache, not both. |
| Scrape Shield → **Email Address Obfuscation** | **Off** | Otherwise `/privacy` and `/terms`, which print support@edgr.in, get a Cloudflare script under `/cdn-cgi/`. |
| Speed → **Rocket Loader** | **Off** | Rewrites and injects scripts. |
| **Web Analytics** / RUM automatic setup, **Zaraz** | **Off** | Injected beacons and third-party tools: `/privacy` says no analytics. |
| Security → Bots → **Bot Fight Mode** | **Off** | Sets the `__cf_bm` cookie. |
| Security level, "I'm Under Attack", custom challenges | No challenges for this name | Challenge pages set `cf_clearance` (and block the Deploy workflow's public checks). |
| Caching → Cache Rules | Hostname is heyp.edgr.in **and** URI path starts with `/api/` → **Bypass cache** | Files and results never sit in Cloudflare's cache (the app also sends `Cache-Control: no-store`). |

If one of these has to stay on, `/privacy` is wrong until `legal/privacy.md` and `legal/glance.md` disclose it — an
owner decision, not a setting. Limits to remember: **100 MB per request** and **100 s to the first byte** (HTTP 524).

## 7. First deploy

### 7.1 Models — before the workflows first run

The app image downloads its ONNX models while it builds and verifies every byte against `scripts/fetch_models.py`.
The models heyp exports itself (Real-ESRGAN, BiRefNet) exist only as assets of the GitHub release **`models-v1`**
(which also carries copies of the third-party ones), so every push to `main` turns CI and Release red until that
release exists. A release needs a commit, so on the empty repository:

1. **Settings → Actions → General → Disable actions**, then push `main`.
2. Put every file named in `MODELS` in one directory: copy them out of an image that already has them
   (`docker create --name heyp-models heyp:dev && docker cp heyp-models:/opt/heyp/models ./models && docker rm heyp-models`),
   or rebuild ours (Real-ESRGAN byte for byte, BiRefNet by parity) with `scripts/export_models/export_all.sh ./models` (the optional int8 BiRefNet
   included) and add the third-party ones with `python3 scripts/fetch_models.py ./models --with-optional` (it keeps
   verified files and downloads only the rest).
3. `scripts/publish_models.sh ./models` — checks every size and sha256 (nothing is uploaded otherwise), refuses to
   touch an existing release, then runs `gh release create models-v1` with all files: gh uploads them to a draft and
   publishes it last, which works with release immutability. `--dry-run` prints the command; `--required-only`
   leaves out the optional model (it cannot be added later).
4. Allow actions again and push a commit (`git commit --allow-empty -m "ci: first run" && git push`). CI's
   **Model sources** job (`python3 scripts/fetch_models.py --probe`) fails with one clear error while any required
   model has no source.

Instead of 1–3 you can point the repository variable `HEYP_MODELS_MIRROR` at another copy (comma-separated base URLs)
before the first push, and publish the release later.

### 7.2 Images, then the deploy

1. Merge to `main`. The **Release** workflow publishes `ghcr.io/uroy80/heyp`, `heyp-renderer` and `heyp-egress`.
   The first push creates the packages as **private**: open each one (github.com/uroy80?tab=packages) → Package
   settings → Change visibility → **Public**.
2. Actions → **Deploy** → Run workflow → `main`. The log shows the attestation check, `heyp-deploy deploy` and the
   public checks.
3. Open `https://heyp.edgr.in/` and `/privacy` — read the privacy policy once, it is rendered from your settings.
   Then set `DEPLOY_ENABLED=true`: every push to `main` now deploys by itself.

## 8. Check it

```bash
curl -sI https://heyp.edgr.in/ | grep -i '^set-cookie' || echo "no cookies"     # "no cookies"
curl -s https://heyp.edgr.in/privacy | grep -c '/cdn-cgi/'                       # 0
curl -sI https://heyp.edgr.in/pdf/merge | grep -i '^location'                    # https://heypdf.edgr.in/merge
curl -s https://heyp.edgr.in/api/config | grep -o '"sibling_url":"[^"]*"'         # https://heypdf.edgr.in
curl -s https://www.cloudflare.com/cdn-cgi/trace | grep '^ip='                   # your address …
sudo tail -n 3 /var/log/httpd-heyp/access.log                                    # … is the first field here
```

The access log must show your address (not a Cloudflare one), no `?…` and no browser name. The Deploy workflow
repeats the first two checks after every deploy and fails (without rolling back) if Cloudflare adds a cookie or a
script to `/privacy`. heypdf used to live under `/pdf/` on this name: those addresses now answer 308 to the same path
on `HEYP_SIBLING_URL` (`HEYP_SIBLING_URL=off` turns the link and the redirects off; the redirect answers 404 then).

## 9. Day to day

| Task | How |
|---|---|
| Ship a change | Merge the PR. CI → Release → Deploy run by themselves. |
| Cut a release | After the Release run for the commit finished: `git tag -s v2.1.0 && git push origin v2.1.0`. The tag workflow points `2.1.0`, `2.1`, `2`, `latest` at the same image digests and creates the GitHub Release. |
| **Roll back** | Automatic when a new release is unhealthy on the server (exit 2) or fails the public checks. By hand: Actions → Deploy → an older commit SHA or `v*` tag; or on the server `sudo -u deploy /usr/local/sbin/heyp-deploy rollback` (previous release, images still on disk). |
| Status / logs | `sudo -u deploy /usr/local/sbin/heyp-deploy status`; `docker logs --tail 100 heyp-app-1` (also `heyp-renderer-1`, `heyp-egress-1`). |
| Change settings | Edit `/etc/heyp/heyp.env`, then `sudo -u deploy /usr/local/sbin/heyp-deploy deploy "$(cat /srv/heyp/state/current)" --keep-ci-env` (`--keep-ci-env` keeps the `PROD_ENV` values the last CI deploy sent; a run from a terminal keeps them too). |
| Change a value sent by CI | Edit the `PROD_ENV` secret and redeploy. To remove one, delete that line (or the whole `PROD_ENV` secret) and redeploy — every CI deploy replaces `/srv/heyp/state/ci.env` with exactly what `PROD_ENV` holds, so an empty secret deletes the file. |
| Update `heyp-deploy` / the egress guard | Rerun `bootstrap-vps.sh` from a fresh checkout (idempotent; the CI key cannot change these files). |
| Rotate the deploy key | New key pair → rerun bootstrap with `--ci-key` → update `DEPLOY_SSH_KEY`. |
| OS and Docker updates | `dnf-automatic` with `upgrade_type = security` for security fixes; Docker deliberately. Base images and dependencies arrive as Dependabot PRs. |
| Cloudflare ranges | `heyp-refresh-cf-ips` runs monthly from cron; run it by hand when Cloudflare announces new ranges. |

**Backups: none needed.** Uploads live in RAM for at most `HEYP_TTL_MINUTES` after last use (hard limit
`HEYP_HARD_TTL_MINUTES` after upload) and vanish on restart. Images come from GHCR, deploy files from git, settings
from `/etc/heyp/heyp.env` (keep a copy in your password manager), the TLS certificate from `/etc/ssl/edgr/`. heyp
keeps no state of its own.

## 10. Logs and the privacy policy

`/privacy` promises what the logs contain; keep the deployment matching it (tests/test_legal.py checks the shipped
Apache configuration):

- **App:** no access log in hosted mode (`uvicorn --no-access-log`); error logs never contain file names.
- **Apache** (`deploy/apache/`): `%a` (the visitor's IP via mod_remoteip; `%h` would be Cloudflare's), time, host,
  method, `%U` (the path without the query string), status, size, duration — no headers — in every heyp vhost's own
  `CustomLog`, plus an `ErrorLogFormat` without the referrer Apache's default error format appends. Logs go to
  `/var/log/httpd-heyp/`, which `logrotate-heyp` rotates daily and deletes after 14 days; the stock
  `/etc/logrotate.d/httpd` rule (weekly, 4 copies) covers only `/var/log/httpd/`. A heyp vhost **without** its own
  `CustomLog` would log to the server's `access_log` in the "combined" format — browser, referrer and query strings,
  kept about five weeks — contradicting `/privacy`.
- **Cloudflare**: sees every request (it terminates HTTPS); `/privacy` names it because `HEYP_CDN_PROVIDER` is set.
  Its own records are kept under its terms, not ours.
- **Renderer and egress:** no URLs or host names (`HEYP_EGRESS_LOG_HOSTS=0`).
- **Container logs:** `local` driver, 3 × 10 MB per container.
- **Hostinger backups:** automatic weekly VPS backups keep about two copies, so a log can survive ~14 more days —
  `HEYP_BACKUP_RETENTION_DAYS` prints that on `/privacy` (0 if you turned backups off).

## 11. Contract for the images (what the deploy tooling assumes)

- `heyp` = `Dockerfile` target `runtime`; `heyp-renderer`, `heyp-egress` = `services/<name>/Dockerfile`, stage
  `runtime` (or the last stage). Build args `HEYP_VERSION`, `HEYP_REVISION`. Adding an image means extending the
  matrices in `ci.yml` and `release.yml`, `IMAGES` in `deploy.yml` and a service here (tests/test_delivery.py checks
  they agree).
- The app serves `GET /api/health` on 8080 (`{"status":"ok", …}`; a `"revision"` field is checked when present); the
  renderer listens on `/run/renderer/r.sock` (shared volume) and answers `GET /healthz` there. Settings come from
  `HEYP_*` variables only.
- Non-root user; runs with a read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`, `/tmp` as the only
  writable path (CI starts every image this way, then the whole stack).
- Images carry the `org.opencontainers.image.revision` label; `heyp-deploy` refuses containers whose label is not the
  deployed commit.

## 12. Troubleshooting

| Symptom | Look at |
|---|---|
| "Deploy skipped" notice | The `production` environment lacks `DEPLOY_HOST`, `DEPLOY_SSH_KEY` or `DEPLOY_KNOWN_HOSTS`. |
| `Host key verification failed` | `DEPLOY_KNOWN_HOSTS` does not match (VPS rebuilt?) — re-print it with bootstrap and re-check the fingerprint. |
| `commit … is not on main` | Only commits on `main` or `v*` tags deploy. |
| `images for … not available` | The Release run for that commit has not finished (or failed). Nothing on the server changed. |
| exit 2 "rolled back" | The new release did not become healthy; the job log shows `docker compose ps` and the last 60 log lines. |
| `required variable HEYP_… is missing` | Set it in `/etc/heyp/heyp.env` (operator name/e-mail, domain, egress settings). |
| app restarts with "hosted mode needs …" | `HEYP_OPERATOR_NAME` / `HEYP_OPERATOR_EMAIL` are empty or invalid. |
| `Pool overlaps with other one on this address space` | Another stack (heypdf?) uses heyp's subnets: change `HEYP_EDGE_SUBNET` / `HEYP_EGRESS_SUBNET` (§4.4). |
| `network … gateway_mode_ipv4` errors | Docker Engine < 28 — upgrade Docker. |
| HTML → image "unavailable" | `render` not in `COMPOSE_PROFILES`; renderer sandbox (§4.7); `HEYP_EGRESS_DENY_CIDRS`/`HEYP_EGRESS_SECRET` set? |
| 502 for ~1 s during deploys | Expected: the app container is replaced in place (no blue/green). |
| 502/503 from Apache all the time | The app is not on 127.0.0.1:8180 (`docker ps`), or `HEYP_PORT` and the `ProxyPass` lines disagree. |

## 13. Why not Hostinger's Docker Manager or `hostinger/deploy-on-vps`?

The action posts the compose file's URL to the Hostinger API with an account-wide API token (it can also rebuild,
stop or delete the VPS) and returns as soon as the deploy is *initiated* — no health check, no rollback. heyp's SSH
path needs no Hostinger credentials in GitHub, waits for health, rolls back, and the CI key can only deploy reviewed
commits. Don't manage the `heyp` compose project from the Docker Manager UI as well.
