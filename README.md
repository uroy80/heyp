# heyp

**heyp** is a small, self-hosted photo toolbox that runs in one Docker container. Convert iPhone HEIC photos
to JPG or PNG and do the everyday photo jobs in one place: resize, set DPI, crop and auto-crop, compress to
a target size, make passport photos, replace the background, and read or strip metadata. It has a quiet,
newspaper-style web UI and nothing leaves your machine.

```
docker compose up -d --build   →   http://localhost:8080
```

## Features

- **Open anything**: HEIC/HEIF, JPEG, PNG, WebP, AVIF, TIFF, BMP, GIF (first frame), ICO and JPEG 2000.
- **Convert** to JPEG, PNG, WebP, AVIF, HEIC, TIFF, BMP, GIF or PDF (several images can go into one PDF).
  Output is correct by default:
  - EXIF orientation is applied exactly once.
  - Wide-gamut photos are converted to sRGB: Display-P3 ICC profiles, and the colour info of HEIC/AVIF files
    that carry no profile (HDR PQ/HLG files are left as they are, not tone-mapped).
  - Transparency is flattened onto a colour you choose for formats that can't store it; palette formats keep
    fully transparent and fully opaque pixels exact.
  - DPI is written wherever the format supports it.
  - HEIC quality is mapped so that quality 90 gives files about as large as JPEG at 90.
- **Resize**:
  - Modes: fit, fill, pad, stretch, percent, long or short edge, megapixels.
  - Print sizes in in/cm/mm at any DPI, or change the DPI only without resampling.
  - Presets for screens, social media, A4 and 4×6 in prints.
  - "Don't enlarge": small photos stay as they are; with Pad the canvas is still padded to the exact size.
- **Crop**:
  - A free box or fixed ratios (1:1, 4:5, 3:2, 16:9, 35:45 and more), snapped to whole pixels, per photo.
  - **Auto trim** removes uniform borders, **Smart crop** picks the crop by saliency, **Face crop** centres on
    the largest face. **Apply to all** copies a crop to every photo.
- **Compress to a target size**, e.g. "under 50 KB" for exam and visa forms. It searches quality first and
  only downscales if it has to, then shows the quality and scale it used. Photos above 12 MP search on a small
  proxy, so even 48 MP targets take seconds.
- **Passport and ID photos**:
  - 20 presets checked against official government sources: US (passport/visa and DV lottery), UK, Schengen,
    France, Netherlands, India (passport, visa, OCI), China, Japan, South Korea, Singapore, Malaysia, Canada
    (passport, online renewal, visa), Australia, New Zealand (visa), South Africa. Plus custom sizes.
  - Face-aware framing to each preset's head-size and eye-line rules, measured to the top of the skull where the
    authority says so (Canada, France, Korea, Schengen, UK).
  - Head levelling: a tilt of 1–15° is straightened automatically.
  - Guides on the picture show the allowed head and eye bands and where your photo lands.
  - White or coloured background replacement, off by default for countries that forbid edited backgrounds
    (Canada, India, Korea, Singapore, UK), with a warning if you turn it on anyway.
  - Print sheets for 4×6 in, 5×7 in, 10×15 cm, 13×18 cm, A5, A4 and Letter, with cut lines, at the preset's
    print resolution.
- **Background**: remove it (transparent PNG/WebP) or replace it with a colour, using on-device person
  segmentation (U²-Net).
- **Edit**: rotate, flip, straighten, brightness, contrast, saturation, sharpness, grayscale and auto-contrast.
- **Info**: dimensions, print size, colour profile, camera details, a GPS warning and the full EXIF table.
  Metadata and GPS are removed on export by default.
- **Batch**: drop many files at once. Every download is a single request that comes back as one file, a ZIP,
  or one combined PDF, per-photo crops included. Warnings for any file in the batch are shown afterwards.

## Quick start

You need Docker with Compose v2.

```bash
docker compose up -d --build                     # first build downloads ~180 MB of ONNX models (sha256-checked)
open http://localhost:8080                       # or just visit it in a browser
docker compose logs -f                           # optional: follow logs
docker compose down                              # stop (uploads are wiped)
```

To use a different port, run `HEYP_PORT=9000 docker compose up -d`.

**Using the UI.** The page reads like a newspaper front page: the photo is "Fig. 1", the tools are numbered
sections 01–08 on the left (the numbers are their keyboard shortcuts), and Export is pinned at the foot of the
options panel. Drop photos onto the page, pick them with **Add photos**, or paste with ⌘/Ctrl V. The canvas
shows a live preview at full-resolution quality, and the line below it shows the result's size, file size and,
when compressing, the quality used. Hold **Space** (or the Compare button) to see the original. **Download**
exports the current settings. Removing a photo can be undone for 8 seconds. Light and dark themes follow your
system, or use the toggle.

| Shortcut | Action |
|---|---|
| `1`–`8` | Switch tools |
| `Space` (hold) | Show the original |
| `R` / `Shift R` | Rotate right / left |
| `Ctrl/⌘ S` | Download |
| `Ctrl/⌘ O` | Add photos |
| `[` / `]` | Previous / next photo |
| `Del` | Remove the current photo (with Undo) |
| `Esc` | Revert crop changes |
| Arrows | Nudge the crop box (`Shift` ×10, `Alt` resizes) |
| `?` | Show all shortcuts |

## Security defaults

heyp has **no accounts or authentication**, so the defaults keep it private:

- **Localhost only.** Compose publishes the port on `127.0.0.1`. To reach heyp from other devices on a network
  you trust, opt in with `HEYP_BIND=0.0.0.0 docker compose up -d` (or put `HEYP_BIND=0.0.0.0` in `.env`).
  Anyone who can reach the port can use it.
- **No cross-site use.** Uploads, processing and deletes sent by another website are refused with 403, so a page
  you visit can't drive your heyp. Scripts and `curl` (no `Origin` header) work normally. Set
  `HEYP_ALLOW_CROSS_ORIGIN=1` only if you front heyp with your own origin that rewrites `Host`.
- **No API docs page** unless you set `HEYP_DOCS=1` (`/docs`, `/redoc`, `/openapi.json`).
- **No dotfiles** are ever served (`/.DS_Store`, `/.env` → 404), and none are in the image.
- **Bounded resources.** Each upload is limited in bytes and pixels (checked from the header before decoding),
  heavy jobs run a few at a time within a memory budget, a request that would take more than about an hour of
  processing is refused, and a busy server answers 503 with `Retry-After` instead of piling up work. The container
  has a 4 GB memory limit and a 1 GB RAM-backed `/tmp`.
- **Hardened container.** Non-root user, read-only root filesystem, `no-new-privileges`, a process limit, a
  digest-pinned base image, hash-locked Python packages and sha256-pinned models. The page's
  Content-Security-Policy only allows `'self'`.

## Configuration

Set these in the shell or an `.env` file next to `docker-compose.yml` (compose passes them through), or under
`environment:` for the less common ones.

| Variable | Default | Meaning |
|---|---|---|
| `HEYP_BIND` | `127.0.0.1` | Host address compose publishes on; `0.0.0.0` exposes heyp to your network |
| `HEYP_PORT` | `8080` | Host port compose publishes |
| `HEYP_MEM_LIMIT` | `4g` | Container memory limit. Raise it together with `HEYP_MAX_PIXELS`, `HEYP_MAX_JOBS` or `HEYP_TMP_SIZE` |
| `HEYP_TMP_SIZE` | `1g` | RAM-backed `/tmp` for uploads and ZIPs (counts against the memory limit); full → 507 |
| `HEYP_MAX_UPLOAD_MB` | `100` | Per-file upload limit (413 above it) |
| `HEYP_MAX_FILES` | `200` | Files per upload request and per export |
| `HEYP_MAX_PIXELS` | `50000000` | Largest image (decoded or produced) in pixels, 413 above it. Phones make 12–48 MP |
| `HEYP_TTL_MINUTES` | `60` | Uploads are deleted after this much inactivity |
| `HEYP_MAX_JOBS` | `2` (compose); app default `max(2, min(4, CPUs))` | Concurrent heavy jobs |
| `HEYP_JOB_MEMORY_MB` | `0` = 60 % of the memory limit minus the cache | Memory the running jobs may plan to use together |
| `HEYP_CACHE_MB` | `512` | Memory for decoded images kept between requests |
| `HEYP_CACHE_ITEMS` | `6` | At most this many decoded images are kept |
| `HEYP_ALLOW_CROSS_ORIGIN` | `0` | `1` turns off the cross-origin guard |
| `HEYP_DOCS` | `0` | `1` serves the interactive API docs |
| `HEYP_TMP_DIR` | `/tmp/heyp` | Upload store, on the tmpfs |
| `HEYP_MODELS_DIR` | `/opt/heyp/models` | ONNX models; both are baked into the image |
| `HEYP_PREVIEW_MAX` | `1600` | Largest side of preview images |

**Memory sizing** (measured): the process with its models takes about 0.45 GB, caches up to about 0.9 GB, and
each running job about 0.9 GB for a 12 MP background removal up to 2.25 GB at 50 MP (other operations need much
less). 4 GB fits two 50 MP background removals at once. If heyp goes over its limit only heyp is restarted, which
wipes uploads. On a smaller host lower `HEYP_MAX_PIXELS` or `HEYP_MAX_JOBS` instead.

Note that compose sets `HEYP_MAX_PIXELS=50000000` and `HEYP_MAX_JOBS=2` explicitly; a plain `docker run` of the
image uses the app defaults above.

If the models are missing, heyp degrades gracefully. `/api/health` reports `face`/`segmentation: false`,
passport framing falls back to a centred crop with a warning that says detection is unavailable, and background
replacement returns 503.

## API

The UI uses the same JSON API, so you can script heyp too. Errors always look like `{"detail": "..."}`:

| Status | Meaning |
|---|---|
| 400 | Empty upload |
| 403 | Cross-origin request (see Security defaults) |
| 404 | Unknown or expired id, or no such path |
| 413 | Too large: file, pixels, request body, ZIP, or too much work for one request |
| 415 | Not an image heyp can read |
| 422 | Invalid request or op |
| 499 | You closed the connection during a ZIP/PDF batch |
| 503 | Busy (retry after `Retry-After` seconds) or a feature is unavailable |
| 507 | The server's upload storage is full |

| Method & path | What it does |
|---|---|
| `GET /api/health` | Version, features (`heic`, `avif`, `face`, `segmentation`) and `limits` (`max_upload_mb`, `max_files`, `max_pixels`) |
| `GET /api/formats` | Input and output formats |
| `GET /api/presets/passport` | Passport presets (official source URLs, `background_edit_allowed`, `crown_includes_hair`) and papers |
| `POST /api/files` | Upload (multipart field `files`, repeatable) → `{files: [FileInfo], errors: [...]}` |
| `GET /api/files/{id}` | FileInfo including the full readable EXIF |
| `GET /api/files/{id}/preview?max=1600` | Oriented sRGB JPEG preview (PNG if the image has transparency) |
| `GET /api/files/{id}/faces` | Detected faces with landmarks |
| `DELETE /api/files/{id}` | Delete an upload now (204) |
| `POST /api/preview` | Run ops + encode one file → size, dimensions, warnings, a small data-URL image, `encode` and `passport` |
| `POST /api/process` | Run ops + encode → the file; several ids → ZIP (or one PDF) |

A process request is a list of `ops` applied in order, plus `output` options. `ops_by_id` gives some files their
own op list (it replaces `ops` for those ids), which is how per-photo crops go into one ZIP:

```json
{
  "ids": ["<id1>", "<id2>"],
  "ops": [{"op": "resize", "mode": "long_edge", "size": 1600}],
  "ops_by_id": {"<id2>": [{"op": "crop_aspect", "ratio": "4:5", "anchor": "face"}]},
  "output": {"format": "jpeg", "quality": 85, "max_kb": 300, "strip_metadata": true}
}
```

The ops are `rotate`, `flip`, `crop`, `crop_aspect`, `trim`, `resize`, `pad`, `adjust`, `background`,
`passport` (with `level` for head levelling) and `sheet` (its `dpi` defaults to the passport preset's). The
output options are `format` (`jpeg|png|webp|avif|heic|tiff|bmp|gif|pdf|original`), `quality`, `lossless`,
`progressive`, `chroma`, `dpi`, `strip_metadata`, `strip_gps`, `colorspace`, `background`, `max_kb`, `min_kb`,
`combine_pdf` and `filename`. [PLAN.md §3](PLAN.md) has every field and limit.

Examples (run from the folder holding your photo):

```bash
# upload (the response holds the id)
ID=$(curl -s -F files=@IMG_0001.HEIC http://localhost:8080/api/files | sed -E 's/.*"id":"([0-9a-f]{32})".*/\1/')

# HEIC → JPG (EXIF orientation applied, GPS removed)
curl -s -X POST http://localhost:8080/api/process -H 'Content-Type: application/json' \
  -d "{\"ids\":[\"$ID\"],\"output\":{\"format\":\"jpeg\",\"quality\":90}}" -o photo.jpg

# 4×6 in print at 300 dpi, then compress under 200 KB
curl -s -X POST http://localhost:8080/api/process -H 'Content-Type: application/json' \
  -d "{\"ids\":[\"$ID\"],\"ops\":[{\"op\":\"resize\",\"mode\":\"fill\",\"width\":4,\"height\":6,\"unit\":\"in\",\"dpi\":300}],
       \"output\":{\"format\":\"jpeg\",\"max_kb\":200}}" -o print.jpg

# what would "under 50 KB" do? (size, quality used, warnings; no download)
curl -s -X POST http://localhost:8080/api/preview -H 'Content-Type: application/json' \
  -d "{\"ids\":[\"$ID\"],\"output\":{\"format\":\"jpeg\",\"max_kb\":50}}" | sed -E 's/"image":"[^"]*"/"image":"…"/'

# US passport photo on a white background, tiled on a 4×6 in sheet (6 copies)
curl -s -X POST http://localhost:8080/api/process -H 'Content-Type: application/json' \
  -d "{\"ids\":[\"$ID\"],\"ops\":[{\"op\":\"passport\",\"preset\":\"us\"},{\"op\":\"sheet\",\"paper\":\"4x6\"}]}" -o sheet.jpg

# delete when done (otherwise it expires after HEYP_TTL_MINUTES)
curl -s -X DELETE http://localhost:8080/api/files/$ID
```

Single-file responses carry `X-Heyp-Width`, `X-Heyp-Height` and `X-Heyp-Warnings` (a URL-encoded JSON list,
e.g. "No face detected — used a centred crop; …"). ZIP and combined-PDF responses carry `X-Heyp-Warnings` too,
with each entry prefixed by the file name, when any file had a warning.

## Privacy

- **Everything is processed inside the container.** There are no CDNs, analytics or external calls; the web
  fonts are bundled, and onnxruntime telemetry is disabled.
- **Uploads stay on the container's tmpfs.** Each one is stored under a random id and deleted when you
  remove it, when you close the tab, after `HEYP_TTL_MINUTES` of inactivity, and on every restart.
- **Location and metadata are removed by default.** Exports drop EXIF metadata, GPS included. If you keep
  metadata, GPS is still removed unless you turn that on too.
- **Logs record requests, not content.** Logs contain request lines with opaque ids, never file names or
  image data. Health-check polls are not logged.

## Development

Python runs in Docker, so you don't need a local toolchain. The `dev` target has pytest and httpx, and the
source is bind-mounted. In the dev image `HEYP_REQUIRE_MODELS=1` makes model-dependent tests fail rather than
skip when a model is missing.

```bash
docker build --target dev -t heyp:dev .
docker run --rm -v "$PWD":/app -w /app heyp:dev pytest -q                  # full suite (~1,000 tests, ~1 min)
docker run --rm -v "$PWD":/app -w /app heyp:dev pytest -q tests/test_api.py
docker run --rm -p 127.0.0.1:8099:8080 -v "$PWD":/app -w /app heyp:dev \
  uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload                 # live-reload dev server
node --test static/tests/*.test.mjs                                        # UI unit tests
for f in static/app.js static/js/*.js static/js/tools/*.js; do node --check "$f"; done
scripts/smoke.sh                                                           # build + check the runtime image
```

The frontend is plain ES modules with no build step. Edit `static/` and reload. Python dependencies are locked
with hashes: edit `requirements*.txt`, then run `scripts/lock.sh` (`scripts/lock.sh --base` prints the current
base-image digest for the `Dockerfile`).

## Architecture

```
Browser (vanilla HTML/CSS/JS from /static, self-hosted fonts)
   │  fetch JSON / multipart
   ▼
FastAPI  app/main.py (security headers, cross-origin guard, body caps, static, lifespan cleanup)
         app/api.py  (/api routes, job admission: slots + memory budget + bounded queue, work budget)
   │        app/store.py  TTL'd upload store + decoded-image LRU   app/schemas.py  pydantic ops/output
   ▼
app/imaging/pipeline.py   plan → load → ops… → encode
   ├── io.py        decode/encode every format, orientation, colour management, metadata, PDF
   ├── ops.py       rotate, flip, crop, crop_aspect, trim, resize, pad, adjust, smart crop
   ├── compress.py  encode to a target size (quality search, proxy for large images, then downscale)
   ├── info.py      FileInfo / EXIF summary
   ├── face.py      YuNet face detection (OpenCV), pupil refinement
   ├── segment.py   U²-Net person segmentation (onnxruntime) + compositing
   ├── presets.py   passport presets with official sources and rule flags, print papers
   └── passport.py  face-aware framing, levelling, background replacement, print sheets
```

## Roadmap

[PLAN.md](PLAN.md) is the full spec and design contract (v1.1). Its §11 lists what comes next: watermarks,
collages, RAW (DNG/CR2/NEF), AI upscaling, a document-scan mode, signature cleanup, a PWA, a CLI wrapper,
optional basic auth, HDR tone mapping and more passport presets.
