# heyp :)

**heyp** is an image toolbox — free, open-source (AGPL-3.0) and self-hostable, with a quiet newspaper-style
("Broadsheet") web UI. Convert iPhone HEIC photos, resize, crop, compress to a target size, make passport photos,
remove backgrounds, upscale, watermark, blur faces, make memes and GIFs, and capture web pages as images. It runs at
[heyp.edgr.in](https://heyp.edgr.in); its sibling for PDFs, [heypdf](https://github.com/uroy80/heypdf), is a separate
app at [heypdf.edgr.in](https://heypdf.edgr.in).

```
docker compose up -d --build   →   http://localhost:8080
```

Run it on your own computer (the default **local** mode), or host it publicly (**hosted** mode with a privacy policy,
terms, abuse limits and automatic deletion — see [deploy/DEPLOY.md](deploy/DEPLOY.md)).

## Tools

- **Open anything**: HEIC/HEIF, JPEG, PNG, WebP, AVIF, TIFF, BMP, GIF and animated WebP, ICO, JPEG 2000, PSD/PSB,
  SVG and camera RAW (DNG, CR2, CR3, NEF, ARW, RAF, ORF, RW2, PEF, SRW).
- **Convert** to JPEG, PNG, WebP, AVIF, HEIC, TIFF, BMP, GIF or PDF (several photos → one PDF), correctly by default:
  EXIF orientation applied once, wide-gamut photos converted to sRGB, transparency flattened onto a colour you
  choose, DPI written.
- **Resize** (fit, fill, pad, percent, edges, megapixels, print sizes at any DPI), **crop** (free, fixed ratios,
  auto trim, smart crop, face crop, apply to all), **rotate** (all, or only landscape/portrait photos of a batch).
- **Compress to a target size** ("under 50 KB" for exam and visa forms), or in the original format: SVG
  optimisation, animated-GIF compression and palette PNGs.
- **Upscale** 2× / 4× with Real-ESRGAN (balanced or strong).
- **Remove or replace the background** of any subject (BiRefNet) or of people (U²-Net).
- **Blur faces, number plates or any area**: detection suggests boxes you can adjust; blur, pixelate or solid fill.
- **Watermark** with text or an image — position, rotation, opacity, or a tiled mosaic.
- **Meme generator** on your photo or a blank canvas (Anton captions, outline, auto size).
- **Photo editor**: filters, frames, text, stickers (OpenMoji and our own), shapes and brush.
- **Make GIF** (or animated WebP) from photos: order, delay, loop, size.
- **HTML → image**: capture a web page (or an uploaded HTML file) as JPG/PNG — the optional `render` service.
- **Passport and ID photos**: 20 presets checked against official government sources, face-aware framing to each
  preset's head-size and eye-line rules, head levelling, guides, background replacement where allowed, print
  sheets. Drag the photo under the fixed frame to align it; the dimmed surroundings appear only while you move it.
- **Info**: dimensions, print size, colour profile, camera details, GPS warning, full EXIF. Metadata and GPS are
  removed on export by default.
- **Batch** everything; "Continue with" passes a result to the next heyp tool.

The dateline's "heyp · heypdf" switch (and an "Open heypdf" entry under "Continue with") links to heypdf when
`HEYP_SIBLING_URL` names it. It is a plain link: the two apps share nothing, and no file moves from one to the other.

**Deliberately not included:** face *recognition* (detection finds where a face is, never whose it is), meme
template images we don't own (you upload yours; we ship blank canvases), accounts and cloud storage.

## Quick start

You need Docker with Compose v2 (≥ 2.24).

```bash
docker compose up -d --build                     # the app (the first build fetches ~375 MB of models, sha256-checked)
docker compose --profile render up -d --build    # + HTML → image (renderer + egress proxy)
docker compose down                              # stop (uploads are wiped)
```

The build takes heyp's own model exports (Real-ESRGAN, BiRefNet) from this repository's `models-v1` release; if it
can't reach it, pass a copy with `docker compose build --build-arg HEYP_MODELS_MIRROR=https://…` (deploy/DEPLOY.md
§7.1). To use another port: `HEYP_PORT=9000 docker compose up -d`.

**Using the UI.** The page reads like a newspaper: the photo is "Fig. 1", the tools are numbered sections in the
rail (`1`–`9` jump to the first nine, `?` lists everything), Export is pinned at the foot of the options panel.

| Shortcut | Action |
|---|---|
| `1`–`9` | Switch tools (`?` shows all tools and shortcuts) |
| `Space` (hold) | Show the original |
| `R` / `Shift R` | Rotate right / left |
| `Ctrl/⌘ S` | Download |
| `Ctrl/⌘ O` | Add photos |
| `[` / `]` | Previous / next photo |
| `Del` | Remove the current photo (with Undo) |
| `Esc` | Revert crop changes |
| Arrows | Nudge the crop box (`Shift` ×10, `Alt` resizes) |

## Privacy and legal

- Every tool runs on the heyp server, including face and plate detection, background removal and upscaling (the
  models ship in the image). heyp makes no outbound connections of its own; the only exception is the optional
  renderer, which fetches the web page you ask it to capture through the egress proxy.
- **Local mode** (default): `/privacy` and `/terms` show a short notice that the person running the copy is
  responsible for it, and what the software does.
- **Hosted mode** (`HEYP_MODE=hosted`): `/privacy`, `/terms` and `/credits` are rendered from [`legal/`](legal/) and
  [`CREDITS.md`](CREDITS.md) with the operator's settings, and every claim matches the code and the deployment
  files: no accounts, no cookies, no analytics; uploads live in RAM and are deleted when removed, when the tab
  closes, `HEYP_TTL_MINUTES` after last use and at most `HEYP_HARD_TTL_MINUTES` after upload; no app access log;
  Apache logs without query strings, browser type, referrer or file names, kept 14 days. `tests/test_legal.py`
  checks what can be checked mechanically.
- Exports drop EXIF metadata and GPS by default.
- The whole UI is served from the same origin (CSP `default-src 'self'`); fonts are bundled.

## Security defaults

heyp has **no accounts or authentication**, so the defaults keep a local copy private:

- **Localhost only.** Compose publishes on `127.0.0.1`; opt in to your LAN with `HEYP_BIND=0.0.0.0`.
- **No cross-site use.** Uploads, jobs and deletes sent by another website get 403.
- **Bounded resources.** Bytes, pixels and animation frames (frames × pixels) are checked before decoding; jobs run
  in per-lane queues (`light`, `heavy`, `render`) with memory admission; camera RAW and SVG decoding run in
  resource-limited, confined child processes (no sockets; Landlock where the kernel has it); a busy server answers
  503 with `Retry-After`. Hosted mode adds per-client limits (uploads/hour, jobs and captures/minute, active jobs)
  keyed on a daily-salted hash of the client IP, which is taken from `X-Forwarded-For` only when the request comes
  from `HEYP_TRUSTED_PROXIES`.
- **Hardened containers.** Non-root, read-only root filesystems, `cap_drop: ALL`, `no-new-privileges`, process and
  memory limits, digest-pinned base images, hash-locked Python packages, sha256-pinned models. The renderer reaches
  the internet only through the egress proxy, which refuses private, reserved and the server's own addresses (and
  DNS rebinding).

See [SECURITY.md](SECURITY.md) for the full model and how to report a vulnerability.

## Configuration

Set these in the shell or an `.env` file next to `docker-compose.yml`. The public deployment uses
[`deploy/heyp.env.example`](deploy/heyp.env.example), which documents every setting it changes.

| Variable | Default | Meaning |
|---|---|---|
| `HEYP_MODE` | `local` | `hosted` = public site: legal pages, `no-store` caching, hard TTL, abuse limits, no access log |
| `HEYP_PUBLIC_URL` | — | e.g. `https://heyp.example.com` (required in hosted mode) |
| `HEYP_SIBLING_URL` | `http://localhost:8081` | heypdf, for the "heyp · heypdf" switch and the `/pdf/*` → heypdf redirects; `off` hides both |
| `HEYP_BIND` / `HEYP_PORT` | `127.0.0.1` / `8080` | Where compose publishes the app |
| `HEYP_MEM_LIMIT` / `HEYP_TMP_SIZE` | `4g` / `1g` | Container memory limit and RAM-backed `/tmp` for uploads (counts against it) |
| `HEYP_MAX_UPLOAD_MB` / `HEYP_MAX_FILES` / `HEYP_MAX_PIXELS` | `100` / `200` / `50000000` | Per-file, per-request and per-image limits |
| `HEYP_MAX_REQUEST_MB` | auto (257) | Largest upload request; behind a CDN set it under the CDN's body cap (the UI batches to it) |
| `HEYP_TTL_MINUTES` / `HEYP_HARD_TTL_MINUTES` | `60` / auto (240 hosted, off local) | Delete after inactivity / at most this long after upload |
| `HEYP_MAX_JOBS`, `HEYP_LANE_LIGHT` / `HEAVY` / `RENDER` | `2`; 2 / 1 / 1 | Concurrency |
| `HEYP_MAX_ANIM_PIXELS` / `HEYP_MAX_ANIM_FRAMES` | 400 MP / 1000 | Animation (frame-bomb) limits |
| `HEYP_TRUSTED_PROXIES` | `127.0.0.1/32,::1/128` | Proxies allowed to set the client IP (never `*`). Behind Cloudflare, Apache turns `CF-Connecting-IP` into that IP (`deploy/apache/`, deploy/DEPLOY.md §5) |
| `HEYP_CDN_PROVIDER` / `HEYP_CDN_DPA_URL` | — | A CDN that terminates HTTPS in front of the server (e.g. Cloudflare): named on `/privacy` and `/terms` |
| `HEYP_OPERATOR_NAME`, `HEYP_OPERATOR_EMAIL`, `HEYP_JURISDICTION`, `HEYP_EFFECTIVE_DATE`, … | — | Printed on `/privacy` and `/terms` (name + e-mail required in hosted mode) |
| `HEYP_SOURCE_URL` | `https://github.com/uroy80/heyp` | "Source code" link (AGPL §13) |
| `HEYP_ALLOW_CROSS_ORIGIN` / `HEYP_DOCS` | `0` / `0` | Turn off the cross-origin guard / serve the OpenAPI docs |

**Memory sizing** (measured): the process with its models takes about 0.65 GB; each running heavy job up to
~1.9 GB at 50 MP (background removal and upscaling are the worst). If heyp goes over its limit only heyp restarts,
which wipes uploads; on a smaller host lower `HEYP_MAX_PIXELS` or the lanes.

## API

The UI uses the same JSON API. Errors look like `{"detail": "..."}` (400 empty upload · 403 cross-origin ·
404 unknown/expired · 413 too large · 415 unreadable or not an image · 422 invalid · 429 rate limit · 503
busy/unavailable with `Retry-After` · 507 upload storage full).

| Method & path | What it does |
|---|---|
| `GET /api/health` · `GET /api/config` | Version, features and limits · mode, TTLs, features, limits, operator, source URL, heypdf link |
| `POST /api/files` | Upload images (and HTML pages for the capture tool; multipart `files`, repeatable) → `{files: [FileInfo], errors}` |
| `GET /api/files/{id}` · `/preview` · `/download` · `/faces` · `DELETE` | FileInfo · preview image · the bytes · detected faces · delete now |
| `POST /api/preview` · `POST /api/process` | The image pipeline: run ops + encode → preview / file (several ids → ZIP or one PDF) |
| `GET /api/tools` · `GET /api/tools/{id}` | Every job tool with its options schema, limits and availability |
| `POST /api/jobs` · `GET /api/jobs/{id}` · `GET /api/jobs/{id}/events` · `DELETE /api/jobs/{id}` | Run a tool on files (202) · status and result files · progress as server-sent events · cancel |
| `GET /api/detect/{id}` · `GET /api/assets/catalog` | Suggested blur boxes (faces, plates) · the editor's filters, frames, fonts and stickers |
| `GET /api/presets/passport` · `GET /api/formats` | Passport presets and papers · decoder and encoder lists |

```bash
# HEIC → JPG under 200 KB
ID=$(curl -s -F files=@IMG_0001.HEIC http://localhost:8080/api/files | jq -r '.files[0].id')
curl -s -X POST http://localhost:8080/api/process -H 'Content-Type: application/json' \
  -d "{\"ids\":[\"$ID\"],\"output\":{\"format\":\"jpeg\",\"max_kb\":200}}" -o photo.jpg

# Remove the background as a job, then download the result
JOB=$(curl -s -X POST http://localhost:8080/api/jobs -H 'Content-Type: application/json' \
  -d "{\"tool\":\"img.remove_background\",\"file_ids\":[\"$ID\"],\"options\":{}}" | jq -r .job_id)
OUT=$(curl -s http://localhost:8080/api/jobs/$JOB | jq -r '.result.files[0].id')   # poll until "status":"done"
curl -s -o cutout.png http://localhost:8080/api/files/$OUT/download
```

[PLAN.md](PLAN.md) has the architecture, every op, option and limit.

## Development

Python runs in Docker, so you don't need a local toolchain. See [CONTRIBUTING.md](CONTRIBUTING.md).

```bash
docker build --target dev -t heyp:dev .
docker run --rm --network none -v "$PWD":/app -w /app heyp:dev pytest -q -m "not services"   # ~1,900 tests
node --test static/tests/*.test.mjs                                                          # UI unit tests
scripts/smoke.sh                                                                             # build + check the runtime image
.github/scripts/stack-smoke.sh                                                               # the production stack (after CI-style builds)
```

The frontend is plain ES modules with no build step. Python dependencies are locked with hashes: edit
`requirements*.txt`, then run `scripts/lock.sh` (`services/lock.sh` for the service images).

**CI/CD** (`.github/workflows/`): every pull request runs ruff, shellcheck, actionlint, zizmor, the Python, UI and
service tests, builds the three images (`heyp`, `heyp-renderer`, `heyp-egress`), smoke-tests and scans them, starts
the production stack and checks it from the outside, checks the Apache configuration, and runs CodeQL. Merges to
`main` publish multi-arch images with signed provenance and SBOM to GHCR and deploy them over SSH to a
forced-command wrapper that rolls back on failure ([deploy/DEPLOY.md](deploy/DEPLOY.md)).

## Architecture

```
Browser: static/ — vanilla ES modules, bundled fonts, CSP 'self'
   │ JSON / multipart / server-sent events
   ▼
app (FastAPI, one container)
   app/main.py, app/routers/     security headers, cross-origin guard, legal pages, jobs, tools, config, /pdf/* redirect
   app/api.py                    uploads, previews, exports (the image pipeline over HTTP)
   app/core/                     tool registry, job runner (lanes, queues, SSE), sandbox, abuse limits
   app/imaging/                  pipeline + op plug-ins (ops_ext/), models (YuNet, U²-Net, BiRefNet, Real-ESRGAN, plates)
   app/tools/                    job tools: export, Make GIF, compress, upscale, background removal, blur, HTML → image
   app/legal.py                  /privacy, /terms, /credits from legal/*.md
   └── /run/renderer/r.sock ─────► renderer: Playwright + Chromium (isolated network) ──► egress: allow-list proxy ──► internet
```

## Licence and credits

heyp is free software under the [GNU Affero General Public License v3.0](LICENSE): if you run a modified version for
others, offer them its source. Fonts, models, stickers and engines keep their own licences — see
[CREDITS.md](CREDITS.md) (also served at `/credits`), which also says where to get the source of the GPL programs in
the published images. Security reports: [SECURITY.md](SECURITY.md).

## Roadmap

Saved workflows (chains of tools), a PWA and a CLI wrapper, optional basic auth, HDR tone mapping and more passport
presets — see [PLAN.md](PLAN.md).
