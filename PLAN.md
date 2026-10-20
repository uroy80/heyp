# heyp — architecture & build spec (2.0)

> **heyp** is an image toolbox: HEIC → JPG/PNG conversion and every everyday photo job (resize, resolution/DPI, crop
> & auto-crop, compress to a target KB, passport/ID photos with print sheets, background removal, upscaling,
> watermarks, blurring faces and plates, memes, a photo editor, GIFs, web-page capture, metadata) behind one quiet,
> editorial UI. It runs as a small Docker stack — the app plus an optional renderer and egress proxy — on someone's
> own computer (local mode) or as a public site (hosted mode, heyp.edgr.in).

This file is both the product plan and the **contract** the implementation follows. Module boundaries, function
signatures and the HTTP API below are binding — if code and this file disagree, fix one of them deliberately.

**2.0** adds the job runner and tool registry (long work runs as background jobs with progress over server-sent
events), camera RAW / PSD / SVG inputs, AI models running on the server (Real-ESRGAN upscaling, BiRefNet background
removal, plate detection), the watermark / meme / editor / GIF tools, HTML → image through an isolated renderer,
hosted mode with legal pages and abuse limits, a sandbox for native decoders, and the CI/CD and deploy kit. heypdf,
the PDF toolbox, is a separate app (github.com/uroy80/heypdf); heyp only links to it (§7, §11).

---

## 1. Goals / non-goals

**Goals**
- Open anything a phone or camera produces: HEIC/HEIF, JPEG, PNG, WebP (also animated), AVIF, TIFF, BMP, GIF (also
  animated), ICO, JPEG 2000, PSD/PSB, SVG/SVGZ and camera RAW (DNG, CR2, CR3, NEF, ARW, RAF, ORF, RW2, PEF, SRW).
- Convert to JPEG, PNG, WebP, AVIF, HEIC, TIFF, BMP, GIF, PDF (multi-image → one PDF); animations stay animated
  where the output format can hold them.
- Batch everything (many files in → ZIP or one PDF out), including per-file crops and edits.
- Correct by default: EXIF orientation applied once, wide-gamut (ICC or HEIC/AVIF `nclx`) → sRGB, alpha flattened onto
  a chosen colour for formats without alpha, DPI written where the format supports it, metadata and GPS stripped.
- Passport/visa/ID photos for common countries: automatic face-aware framing to official head-size rules, head
  levelling, background replacement, print sheets (4×6 in, A4, …) with cut lines.
- Every model runs on the heyp server; no file is sent to an outside service. The only outbound traffic is the
  renderer fetching a web page the user asked to capture, through an allow-list proxy.
- Truthful by construction: the UI's and the legal pages' claims are derived from the configuration (§11).
- Quiet, fast, keyboard-friendly UI; no CDN (fonts are self-hosted).
- One command: `docker compose up -d --build` → http://localhost:8080 (loopback only by default).

**Non-goals**: accounts, cloud storage, persistence beyond a TTL'd temp store, face *recognition*, video/Live Photo,
HDR tone mapping, PDF tools (heypdf). (See §15 roadmap.)

---

## 2. Architecture

```
Browser (vanilla ES modules, served from /)
   │  fetch JSON / multipart / server-sent events (same origin only for POST/DELETE)
   ▼
FastAPI  app/main.py      security headers · cross-origin guard · dotfile 404 · body caps · docs gating ·
                          /pdf/* → 308 HEYP_SIBLING_URL · lifespan (cleanup loop, job runner, sandbox setup)
         app/api.py       /api image routes (uploads, previews, exports) · request admission (slots + memory budget)
         app/routers/     /api/jobs · /api/tools · /api/config · /privacy /terms /credits
         app/store.py     FileStore (/tmp/heyp, idle + hard TTL, uuid ids, pins) · decoded-image LRU
         app/core/        registry (job tools) · jobs (lanes, queues, deadlines, SSE) · sandbox + confine · limits
         app/legal.py     legal pages from legal/*.md and CREDITS.md
   │
   ├─ Pipeline (app/imaging/pipeline.py): plan → load → [ops…] → encode
   │    io.py (decode/encode, colour, metadata; RAW and SVG in the sandbox) · ops.py · compress.py · info.py ·
   │    face.py (YuNet) · segment.py (U²-Net) · matting.py (BiRefNet) · upscale.py (Real-ESRGAN) · plates.py ·
   │    blur.py · presets.py · passport.py · text.py · watermark.py · meme.py · editor.py · animated.py ·
   │    opreg.py + ops_ext/ (op plug-ins: upscale, blur_regions, watermark, meme, filter, frame, overlay)
   ├─ Job tools (app/tools/): img.process (export) · img.make_gif · img.compress · img.upscale ·
   │    img.remove_background · img.blur · img.html_to_image
   └─ /run/renderer/r.sock ─► renderer (Playwright + Chromium, render-sbx network) ─► egress (allow-list proxy) ─► internet
```

- **Stack**: Python 3.12, FastAPI 0.143, Pillow 12.3 (native AVIF), pillow-heif 1.8 (HEIC read+write), OpenCV-headless
  5.0 (YuNet), onnxruntime 1.31, numpy 2.5, rawpy (LibRaw), psd-tools, resvg-py, scour, fontTools, plain `uvicorn`
  with uvloop/httptools. gifsicle and pngquant run as separate programs. Every Python dependency is pinned with hashes
  in `requirements.lock` / `requirements-dev.lock` (`scripts/lock.sh`). Frontend: no framework, no build step.
- **Models** (downloaded at image build by `scripts/fetch_models.py` into `$HEYP_MODELS_DIR=/opt/heyp/models`, each
  checked against a pinned size + sha256; any mismatch fails the build): YuNet face detection (230 KB), U²-Net human
  segmentation (176 MB), BiRefNet lite (181 MB; optional int8 variant), Real-ESRGAN general x4v3 and its denoised blend
  (4.9 MB each), YOLOv9-t licence plates (7.8 MB). heyp's own exports are assets of the `models-v1` release
  (`scripts/export_models/` rebuilds them — Real-ESRGAN byte for byte, BiRefNet by parity with PyTorch; `scripts/publish_models.sh` publishes them). At runtime a
  missing model turns its feature off (`/api/health`), the tools that need it report `available: false`.
- **Concurrency**: synchronous image endpoints run in Starlette's threadpool; `app.state.jobs` (`api.JobSlots`) admits
  heavy work first-come-first-served — at most `HEYP_MAX_JOBS` at once, their planned peak memory (`pipeline.plan`)
  within `HEYP_JOB_MEMORY_MB`, and a bounded wait queue; a request that can't queue gets **503 + `Retry-After: 5`**.
  Job tools run on lane workers (§8) and heavy lanes reserve memory from the same budget. Cheap endpoints are `async`.
- **Privacy**: uploads live in `HEYP_TMP_DIR` (a tmpfs in compose) under a random uuid4-hex id, deleted after
  `HEYP_TTL_MINUTES` of inactivity, at most `HEYP_HARD_TTL_MINUTES` after upload (hosted), on `DELETE`, when the page
  that uploaded them closes, and on restart. Error logs never contain file names or contents; in hosted mode the app
  keeps no access log at all.

### Directory layout
```
heyp/
├── Dockerfile               # targets: pybase → models → base → dev (tests) | runtime (default, last)
├── docker-compose.yml       # local stack: app (+ renderer, egress with --profile render)
├── requirements*.txt  → requirements*.lock (hashes)   scripts/{fetch_models.py, lock.sh, smoke.sh, publish_models.sh, export_models/}
├── app/
│   ├── main.py · config.py · api.py · schemas.py · store.py · legal.py
│   ├── core/{registry,jobs,files,limits,sandbox,confine,errors}.py
│   ├── routers/{jobs,tools,config,legal}.py
│   ├── tools/{img_process,img_tools,img_ai,render}.py   # job tools, registered at import
│   ├── clients/{renderer,_http}.py                      # the renderer over its Unix socket
│   ├── imaging/…  (+ ops_ext/)                          # the pipeline, ops and models
│   ├── assets/{fonts,stickers}/ · templates/legal.html
├── services/{renderer,egress}/   # the two sidecar images, each with its own tests and locks; e2e/ SSRF test bed
├── static/{index.html, styles.css, app.js, js/, js/tools/, fonts/}  (+ tests/: node unit tests, not shipped)
├── legal/{privacy,terms,local,glance}.md · CREDITS.md · LICENSE (AGPL-3.0)
├── deploy/  compose.prod.yml · heyp.env.example · apache/ · bootstrap-vps.sh · heyp-deploy · heyp-egress-guard · seccomp/
├── .github/ workflows (ci, release, deploy, relock) · scripts (smoke-image, stack-smoke, validate-deploy, service-tests)
└── tests/   pytest; fixtures generate images incl. HEIC on the fly
```

### Configuration (env)
Settings are read once at start-up (`app/config.py`); invalid values (non-integers, below the minimum, flags other
than 0/1/true/false/yes/no/on/off, bad URLs) fail the start.

| var | default | meaning |
|---|---|---|
| `HEYP_BIND` / `HEYP_PORT` | 127.0.0.1 / 8080 | (compose) where the app is published; `0.0.0.0` exposes it to the LAN — it has **no authentication** |
| `HEYP_MEM_LIMIT` / `HEYP_TMP_SIZE` | 4g / 1g | (compose) container memory limit / RAM-backed `/tmp` (uploads, ZIP spools); full → 507 |
| `HEYP_MODE` | local | `hosted`: legal pages with the operator's details, `no-store` on the API, hard TTL, abuse limits, no access log |
| `HEYP_PUBLIC_URL` | — | public address (required in hosted mode) |
| `HEYP_SIBLING_URL` | http://localhost:8081 | heypdf: the "heyp · heypdf" link and the `/pdf/*` redirects; `off` = neither |
| `HEYP_MAX_UPLOAD_MB` / `HEYP_MAX_REQUEST_MB` | 100 / 0 (auto: max(256, upload) + 1) | per-file upload limit → 413 / largest upload request (≥ upload + 1) |
| `HEYP_MAX_FILES` | 200 | files per upload request and ids per process request |
| `HEYP_MAX_PIXELS` | 50_000_000 | pixel limit for decoded and produced images (header-checked before decoding) → 413 |
| `HEYP_MAX_ANIM_PIXELS` / `HEYP_MAX_ANIM_FRAMES` | 400 000 000 / 1000 | decoded pixels over all frames / frames of one animation |
| `HEYP_TTL_MINUTES` / `HEYP_HARD_TTL_MINUTES` | 60 / auto (240 hosted, 0 = off local) | idle TTL / limit from upload (can't be 0 in hosted mode) |
| `HEYP_TMP_DIR` | /tmp/heyp | upload store |
| `HEYP_MAX_JOBS` / `HEYP_JOB_MEMORY_MB` | max(2, min(4, CPUs)) / 0 = 60 % of the container limit − cache | concurrent heavy work / its memory budget |
| `HEYP_CACHE_ITEMS` / `HEYP_CACHE_MB` | 6 / 512 | decoded-image LRU (items / bytes of pixel data) |
| `HEYP_LANE_LIGHT` / `HEAVY` / `RENDER`, `HEYP_QUEUE_MAX` | 2 / 1 / 1, 32 | job workers per lane, queued jobs per lane (→ 503) |
| `HEYP_SANDBOX_MEM_MB` / `HEYP_SANDBOX_SLOTS` / `HEYP_SANDBOX_CONFINE` | 1024 / 3 / 1 | sandbox child memory, children at once, Landlock + seccomp on |
| `HEYP_LIMITS` | auto (on in hosted mode) | abuse limits; `HEYP_LIMIT_UPLOAD_MB_HOUR` 2048, `HEYP_LIMIT_JOBS_MINUTE` 30, `HEYP_LIMIT_RENDERS_MINUTE` 6, `HEYP_LIMIT_RENDER_HOST_MINUTE` 10, `HEYP_LIMIT_ACTIVE_JOBS` 8 |
| `HEYP_TRUSTED_PROXIES` | 127.0.0.1/32, ::1/128 | peers whose `X-Forwarded-For` is believed (never `*` or /0) |
| `HEYP_ALLOW_CROSS_ORIGIN` / `HEYP_DOCS` | 0 / 0 | 1 disables the cross-origin guard (§3) / mounts `/docs`, `/redoc`, `/openapi.json` |
| `HEYP_MODELS_DIR` | /opt/heyp/models | ONNX models |
| `HEYP_UPSCALE_MAX_INPUT_MP`, `HEYP_MATTING_MODEL`, `HEYP_AI_THREADS`, `HEYP_DETECT_CONCURRENCY` | 6, fp32, from the CPU quota, 1 | model tuning |
| `HEYP_RENDERER_SOCKET` | /run/renderer/r.sock | the renderer (missing socket → HTML → image unavailable) |
| `HEYP_PREVIEW_MAX` | 1600 | max side of preview images |
| `HEYP_SOURCE_URL` / `HEYP_REVISION` | github.com/uroy80/heyp / (build arg) | the AGPL §13 source link / the commit, shown in `/api/health` and the legal footer |
| `HEYP_OPERATOR_NAME`, `HEYP_OPERATOR_EMAIL`, `HEYP_GRIEVANCE_OFFICER`, `HEYP_JURISDICTION`, `HEYP_EFFECTIVE_DATE`, `HEYP_CDN_PROVIDER`, … | — | the legal pages (§11; `deploy/heyp.env.example` lists them all) |
| `HEYP_REQUIRE_MODELS` | 1 in the dev image | tests that need the models fail instead of skipping |

---

## 3. HTTP API (all JSON unless noted; errors are `{"detail": "..."}`)

| method & path | purpose |
|---|---|
| `GET /api/health` | `{"status":"ok","version":"2.0.0","revision":…,"features":{"heic","avif","face","segmentation","upscale","matting","plates","renderer":bool},"limits":{"max_upload_mb","max_files","max_pixels","upscale_max_input_pixels"[,"max_request_mb"]}}` — `max_pixels` is the stricter of the app and decoder limits |
| `GET /api/config` | what the UI needs to describe the deployment truthfully: `mode`, `public_url`, `sibling_url` (null when off), `ttl_minutes`, `hard_ttl_minutes`, `features`, `limits` (+ the abuse limits when on), `operator {name, email}`, `source_url`, `version`, `revision`, `legal` |
| `GET /api/formats` | `{"input":[decoders],"output":["jpeg","png","webp","avif","heic","tiff","bmp","gif","pdf"],"images":["psd","psb","svg","svgz", RAW…],"html":["html","htm"]}` |
| `GET /api/presets/passport` | `{"presets":[Preset…], "papers":[{"id":"4x6","name":"4×6 in","width_mm":101.6,"height_mm":152.4,"borderless":true},…]}`; presets carry `width_px`/`height_px` and their rule flags |
| `POST /api/files[?accept=image\|html]` | multipart, field `files` (repeatable). The kind is decided from the bytes (`app/core/files.py`): images are header-probed and decoded once, HTML pages are stored as they are for the capture tool, anything else is refused (415). → `{"files":[FileInfo…],"errors":[{"name","detail"}…]}` (200 if ≥1 ok; if all fail the status is the most relevant of 507/503/429/413/415/422/400 and the body adds `detail`) |
| `GET /api/files/{id}` | `FileInfo` incl. `exif` (full readable dict) |
| `GET /api/files/{id}/preview?max=1600` | oriented, sRGB, downscaled **image/jpeg** (or **image/png** with alpha). Local mode `Cache-Control: private, max-age=3600`, hosted `no-store`. Only sides 240/480/960/1600 and `HEYP_PREVIEW_MAX` are cached on disk |
| `GET /api/files/{id}/download` | the stored bytes as an attachment (`Content-Security-Policy: …; sandbox`) |
| `GET /api/files/{id}/faces` | `{"width":W,"height":H,"faces":[{"box":[x,y,w,h],"score":f,"landmarks":{…}}]}` in oriented-image px; 503 without a detector |
| `DELETE /api/files/{id}` | 204 (also drops the decoded image and every analysis/mask cache of that upload) |
| `POST /api/preview` | body `ProcessRequest` with exactly one id. Runs the **full-resolution** pipeline and encode (so size is exact) → `PreviewResponse` below |
| `POST /api/process` | body `ProcessRequest`. One id → the file itself; several ids → `application/zip` (or one PDF if `output.format=="pdf"` and `output.combine_pdf`). Headers: `Content-Disposition` (RFC 5987 `filename*`), single file: `X-Heyp-Width`, `X-Heyp-Height`; `X-Heyp-Warnings` (URL-encoded JSON list, capped at ~4 KB). Slow exports go through the `img.process` job instead (§8) |
| `GET /api/tools` · `GET /api/tools/{id}` | the job-tool registry: `{id, title, summary, category, inputs, min_files, max_files, options_schema, limits, lane, engine, timeout_s, outputs, available, reason}` |
| `POST /api/jobs` | `{"tool", "file_ids", "options"}` → 202 `{"job_id", "status", "position"}`; 404 unknown tool, 422 invalid inputs/options, 429 rate limit, 503 tool unavailable or lane queue full (`Retry-After`) |
| `GET /api/jobs/{id}` · `DELETE /api/jobs/{id}` | job state `{id, tool, status, progress, message, warnings, result:{files:[FileInfo], zip:FileInfo\|null, data}, error, position, …}` · cooperative cancel (202) |
| `GET /api/jobs/{id}/events` | server-sent events: `progress` `{progress, message}`, then `done` `{result}` or `error` `{status, detail}`; keep-alive every 15 s; a finished job replays its history |
| `GET /api/detect/{id}?what=faces,plates` | suggested blur boxes `{regions:[{kind,x,y,w,h,shape,score,selected}], width, height}` |
| `GET /api/assets/catalog` · `/stickers/{group}/{name}.svg` · `/fonts/{font}.ttf` | the editor's filters, frames, fonts, shapes and stickers |
| `GET /privacy` · `/terms` · `/credits` | the legal pages (§11) |
| `GET /pdf` · `/pdf/{path}` | 308 to `HEYP_SIBLING_URL/{path}` (query kept; heypdf used to live here); 404 when the sibling is off |

**Status codes**: 400 empty upload · 403 cross-origin state-changing request · 404 unknown/expired id or path ·
413 too large (bytes, pixels, frames, body, ZIP size, or estimated work) · 415 undecodable/unsupported/not an image ·
422 invalid ops/request · 429 abuse limit (`Retry-After`) · 499 client disconnected during a batch · 503 busy
(`Retry-After`) or feature unavailable · 504 job deadline · 507 server storage full · 500 never for user input (500
responses still carry the security headers). Ids are validated against `^[0-9a-f]{32}$` before touching the filesystem.

**Service guards**
- *Cross-origin guard*: unless `HEYP_ALLOW_CROSS_ORIGIN=1`, POST/PUT/PATCH/DELETE with `Sec-Fetch-Site: cross-site`,
  an opaque `Origin` (`null`), or an `Origin` whose host differs from `Host` (or `X-Forwarded-Host`) → 403
  `{"detail":"Cross-origin requests are not allowed"}`. Requests without `Origin` (curl, scripts) pass.
- *Docs*: `/docs`, `/redoc`, `/openapi.json` exist only with `HEYP_DOCS=1` (they are 404 otherwise).
- *Dotfiles*: any path with a segment starting with `.` (`/.DS_Store`, `/.env`) → 404, before routes and static files.
- *Security headers* on every response: CSP `default-src 'self'` (+ `img-src data: blob:`, `frame-ancestors 'none'`;
  downloads add `sandbox`), `nosniff`, `no-referrer`, a Permissions-Policy that turns camera, microphone,
  geolocation, payment and USB off; static files `Cache-Control: no-cache`, the API `no-store` in hosted mode.
- *Body caps* before parsing (declared or chunked): multipart ≤ the request cap above, any other body ≤ 2 MB → 413.
- *Pixel admission*: uploads are header-probed and refused above `HEYP_MAX_PIXELS` before decoding; animations are
  bounded by frames × pixels. Ops/encoders with a high memory cost per pixel have their own caps → 413: background
  50 MP, adjust 100 MP, AVIF output 50 MP, lossless WebP 40 MP, GIF 100 MP. A combined PDF holds at most
  `HEYP_MAX_PIXELS` in total.
- *Work budget*: every item of a request is costed (`pipeline.plan`) before any work; more than ~60 min of estimated
  CPU → 413. Repeated ids are processed once. A ZIP is capped at min(2 GB, half the free space of the upload
  volume) → 413; a full volume → 507. Batches stop between items when the client disconnects (499), and uploads in
  a running batch are pinned so they can't expire mid-way.

### FileInfo
```json
{"id":"…32 hex…","name":"IMG_0001.HEIC","format":"heic","mime":"image/heic","size_bytes":1834221,
 "width":4032,"height":3024,"mode":"RGB","has_alpha":false,"frames":1,"bit_depth":8,
 "dpi":[72,72] | null,"orientation":6,"icc":"Display P3" | null,
 "print_size_in":[13.44,10.08] | null,          // at the file's DPI, null if no DPI
 "camera":{"make":"Apple","model":"iPhone 15 Pro","lens":"…","datetime":"2026:09:01 10:22:01","iso":80,"exposure":"1/120","f_number":1.8,"focal_length_mm":6.8} | null,
 "gps":{"lat":12.97,"lon":77.59} | null,
 "exif":{"Make":"Apple", …},                    // only on GET /api/files/{id}; readable values, bytes dropped
 "kind":"image", "source":"upload", "parent_ids":[], "tool":null,   // "result" files name their inputs and tool
 "expires_at":1760900000.0 | null               // the hard deadline (epoch s) when one applies
}
```
`width`/`height` are **after** orientation is applied (what the user sees). An HTML page carries only `id`, `name`,
`kind:"html"`, `format`, `mime`, `size_bytes` and the origin fields; a combined-PDF result has `kind:"pdf"`, a ZIP
`kind:"other"`.

### ProcessRequest
```json
{
  "ids": ["…"],                    // 1…HEYP_MAX_FILES; duplicates are processed once (first position)
  "ops": [ Op, … ],                // ≤ 32, applied in order; the image is always EXIF-oriented first
  "ops_by_id": {"<id>": [ Op, … ]},// optional: replaces `ops` entirely for that id; keys ⊆ ids (else 422)
  "output": {
    "format": "jpeg",              // jpeg|png|webp|avif|heic|tiff|bmp|gif|pdf|original (aliases jpg, tif, heif)
    "quality": 90,                 // 1–100, lossy formats (HEIC maps 1→1, 90→70, 100→100 onto x265's steeper scale)
    "lossless": false,             // webp/avif lossless
    "progressive": true,           // jpeg
    "chroma": "auto",              // jpeg subsampling: auto(=4:2:0 if q<90 else 4:4:4)|444|422|420
    "dpi": null,                   // int → write DPI (JPEG/PNG/TIFF/PDF/HEIC-exif); null → keep pipeline dpi
    "strip_metadata": true,        // drop EXIF/XMP (always drops GPS when true); ICC handled by colorspace
    "strip_gps": true,             // when strip_metadata=false, still remove GPS
    "colorspace": "srgb",          // srgb (convert via ICC/nclx, embed sRGB) | keep (keep original ICC)
    "background": "#ffffff",       // flatten alpha for formats w/o alpha (jpeg, bmp, pdf; gif keeps 1-bit)
    "max_kb": null,                // target ceiling → compress.encode_to_target (≥ 1 byte after rounding up)
    "min_kb": null,                // floor (best effort, warning if impossible)
    "combine_pdf": true,           // multi-id + pdf → one PDF
    "filename": null               // optional base name override (no extension, sanitized)
  }
}
```
`/api/preview` uses `ops_by_id[id]` when present, else `ops`. The UI always sends a batch export as **one** request:
the most common op list in `ops`, the files that differ (per-file crops) in `ops_by_id`.

### PreviewResponse
```json
{ "width":600, "height":600, "format":"jpeg", "size_bytes":51200, "dpi":[300,300] | null, "warnings":[…],
  "image":"data:image/jpeg;base64,…",                               // ≤ max px display version of the encoded file
  "encode":   {"quality": 72, "scale": 1.0, "met": true} | null,    // when max_kb/min_kb is set, else null
  "passport": null | {
      "preset": "us", "width": 600, "height": 600,                 // output px of the passport frame
      "head_ratio": 0.594, "eye_from_bottom": 0.62, "face_found": true, "roll_deg": 2.1 | null,
      "lines":  {"crown": 52.0, "chin": 409.0, "eye": 228.0} | null,   // y in OUTPUT px (only when face_found)
      "hair_top": 41.5 | null,                                     // top of the hair, OUTPUT px (≠ crown when hair is excluded)
      "bands":  {"head_min": 0.50, "head_max": 0.69, "eye_min": 0.5625|null, "eye_max": 0.6875|null,
                 "top_min": null, "top_max": null},                // preset ranges (fractions of height, eye from bottom)
      "background_edit_allowed": true,
      "source": null | {"width": 3024, "height": 4032,             // the image ENTERING the passport op (full res)
                 "center": [1480.25, 1702.5], "size": [700.0, 900.0], // frame centre / size in its px (2 dp)
                 "angle": 3.1}                                     // frame rotated clockwise (= levelled_deg)
  }
}
```
`source` = `passport.make()`'s `source_frame` (§5.3 step 8): output point (u, v) samples the source at
`center + R(angle)·((u − width/2)·fw/width, (v − height/2)·fh/height)`, `R(θ) = [[cos θ, −sin θ], [sin θ, cos θ]]`.
The UI's passport context view lays the pre-passport photo around a fixed frame with it (§7), and uses `lines`,
`hair_top` and `bands` to predict where a head-size change re-frames the photo (§5.3 steps 2–4).
`passport` is non-null only when the pipeline contains a `passport` op **and no later op changes geometry**
(`adjust` and `background` keep it; sheet, resize, crop, rotate, pad … clear it). Its pixel values then match the
preview's full-res `width`×`height`. Large outputs (> 12 MP) are not decoded again at full size for the display image.

### Ops (discriminated on `"op"`; all pixel values refer to the image **as it is at that step**)
| op | fields | semantics |
|---|---|---|
| `rotate` | `angle: float −360…360`, `expand=true`, `fill="#ffffff"\|"transparent"` | degrees, **clockwise-positive** (UI "rotate right" = +90); multiples of 90 → lossless transpose; other angles → bicubic with `expand`/`fill` |
| `flip` | `direction: "horizontal"\|"vertical"` | mirror |
| `crop` | `x,y,width,height: float`, `unit: "px"\|"fraction"` = "px" | fraction ∈ [0,1] relative to current size; clamped to bounds; ≥1 px result |
| `crop_aspect` | `ratio: "W:H"\|float` (1:100…100:1), `anchor: "center"\|"smart"\|"face"` = "center" | largest crop of that ratio; `smart` = saliency/entropy-weighted position; `face` = centred on largest face (falls back to smart, with a warning) |
| `trim` | `tolerance: int 0–255 = 12`, `padding: int px = 0` | auto-crop uniform borders (colour sampled from the 4 corners; alpha=0 also counts as border) |
| `resize` | `mode`, `width?`, `height?`, `scale?`, `size?`, `megapixels?`, `unit: px\|in\|cm\|mm = px`, `dpi: int = 300`, `upscale = true`, `resample: lanczos\|bicubic\|bilinear\|nearest = lanczos`, `background = "#ffffff"` | see below; any side ≤ 30 000 px |
| `pad` | `top,right,bottom,left: int px`, `color = "#ffffff"` | add border |
| `adjust` | `brightness,contrast,saturation,sharpness: float 0–10 = 1.0`, `grayscale=false`, `auto_contrast=false` | PIL ImageEnhance semantics (1.0 = unchanged) |
| `background` | `mode: "remove"\|"replace"`, `color = "#ffffff"` | person segmentation; `remove` → RGBA; 503 without the model |
| `passport` | `preset: str`, `custom: CustomPreset?`, `background: str\|null = preset default`, `replace_background = true`, `face_index = 0`, `zoom = 1.0` (0.5–2), `offset_x = 0.0`, `offset_y = 0.0` (−1…1), `level = true` | §5 |
| `sheet` | `paper = "4x6"`, `orientation: auto\|portrait\|landscape = auto`, `photo_width_mm?`, `photo_height_mm?` (5–500), `dpi: int\|null = null` (72–1200), `gap_mm = 2`, `margin_mm = 4`, `cut_lines = true`, `copies = 0` (0–200; 0 = as many as fit, at most 200) | tile current image onto paper (§5.4) |
| `upscale` | `factor: 2\|4`, `model: balanced\|strong` | Real-ESRGAN, tiled; inputs up to `HEYP_UPSCALE_MAX_INPUT_MP` |
| `blur_regions` | `regions: [{x,y,w,h (fractions), shape}]`, `mode: blur\|pixelate\|solid`, `strength`, `color` | blur, pixelate (never below 40 %) or cover areas |
| `watermark` | `kind: text\|image`, `text`, `font`, `size`, `color`, `bold`, `shadow`, `opacity`, `rotation`, `position` (9 anchors, `mosaic`, or `custom` with `x`/`y`), `margin`, `spacing`, `image_id`, `image_scale` | text or logo watermark, single or tiled |
| `meme` | `top`, `bottom`, `font`, `size`, `color`, `outline`, `outline_width`, `uppercase`, `layout: inside\|outside`, `band_color` | captions (Anton by default), auto size |
| `filter` | `name`, `intensity` | the photo editor's filters |
| `frame` | `style`, `color`, `width`, `background` | borders and frames |
| `overlay` | `items: [text \| sticker \| shape \| brush]` | the photo editor's layers, positioned in fractions of the image |

The v2 ops are plug-ins (`app/imaging/opreg.py`, `app/imaging/ops_ext/`): each registers a pydantic model, a
still-image apply, a per-frame apply for animations, a cost for `pipeline.plan` and its memory per pixel. The exact
fields and bounds are the models' own (`GET /api/tools` shows the job tools' schemas).

`CustomPreset`: `width_mm`, `height_mm` (≤ 500), `dpi = 300`, `pixel_width?`/`pixel_height?` (together),
`head_min = 0.5`, `head_max = 0.69`, `eye_min?`/`eye_max?` (together), `background = "#ffffff"`.

**Resize modes**
- `fit` – contain inside `width`×`height`; if only one of them is given, the other follows the aspect ratio.
- `fill` – cover `width`×`height` then centre-crop (both required).
- `pad` – contain then pad to exactly `width`×`height` with `background` (both required).
- `exact` – stretch to `width`×`height` (both required).
- `scale` – `scale` percent (e.g. 50).
- `long_edge` / `short_edge` – that edge becomes `size`.
- `megapixels` – total ≈ `megapixels` × 1e6, aspect kept.
- `unit` ≠ px converts `width/height/size` with `dpi` (print size) **and sets pipeline dpi**.
- `upscale=false` never enlarges the photo: `fit`, `fill`, `exact`, `scale`, `long_edge`, `short_edge` and `megapixels`
  return the original size when the target is larger, and `pad` keeps the requested canvas with the unscaled content
  centred (the canvas is still exactly `width`×`height`).
Results are rounded to ints and are always ≥ 1 px. Invalid combos → 422 with a clear message.

---

## 4. Imaging core (module contracts)

```python
# app/imaging/io.py
FEATURES: dict[str, bool]                        # {"heic","avif"} filled by register_plugins()
DEFAULT_MAX_PIXELS = 50_000_000                  # = app.config default; HEYP_MAX_PIXELS overrides
def register_plugins() -> dict[str, bool]        # idempotent; pillow_heif.register_heif_opener(); sets MAX_IMAGE_PIXELS
def max_pixels() -> int
class ImageError(Exception): status: int         # 413 / 415 mapping for the API
@dataclass
class Loaded:
    image: Image.Image          # first frame, EXIF-oriented ONCE, mode ∈ {RGB, RGBA, L, LA}
    format: str                 # canonical lowercase: heic, jpeg, png, webp, avif, tiff, bmp, gif, ico, jp2
    exif: bytes | None          # orientation tag reset to 1
    icc_profile: bytes | None   # embedded ICC, or one built from the HEIC/AVIF nclx colour info
    dpi: tuple[float, float] | None
    frames: int
    bit_depth: int
    orientation: int            # original EXIF orientation (1 if none)
def load(source: str | bytes | BinaryIO, *, name: str | None = None) -> Loaded   # SVG, PSD/PSB, RAW by their bytes
def save(img, fmt, *, quality=90, lossless=False, progressive=True, chroma="auto", dpi=None,
         exif=None, icc_profile=None, strip_metadata=True, strip_gps=True, colorspace="srgb",
         background="#ffffff") -> bytes
def to_srgb(img, icc_profile) -> Image.Image     # no-op if no/unknown profile
def flatten(img, color) -> Image.Image           # alpha → solid colour
def preview(img, icc_profile=None, max_side=1600) -> tuple[bytes, str]   # (bytes, mime)
def mime_for(fmt) -> str ; def ext_for(fmt) -> str ; def canonical_format(name) -> str
def save_pdf(images: list[Image.Image], *, dpi=300, background="#ffffff") -> bytes   # multi-page PDF (flattened RGB pages)
```
Rules:
- HEIC orientation — pillow-heif already applies `irot/imir`; a test verifies orientation 6 is not applied twice.
- CMYK/P/I;16/1-bit inputs are normalised to RGB(A)/L(A); 16-bit gray honours its gamma; 16-bit/HDR HEIC → 8-bit.
  GIF/ICO/multi-page TIFF → first frame. JPEG output never carries alpha.
- Colour: an embedded ICC wins. HEIC (pillow-heif) and AVIF (`colr`/`nclx` box in the first 64 KB) without one get a
  profile built from their nclx primaries + transfer curve (Display P3, DCI-P3, BT.2020 …); sRGB and PQ/HLG (HDR) are
  left alone — HDR is not tone-mapped.
- `save(..., fmt="heic")` uses pillow-heif with the quality remap above; `fmt="pdf"` uses Pillow's PDF writer with
  `resolution=dpi or 300`. TIFF with kept EXIF passes libtiff only safe IFD0 tags (LZW kept). JPEG at high quality
  sizes its output buffer up front (no "Suspension" retries). WebP above 20 MP uses libwebp method 2 (falls back to
  4), ~8× faster at ~8 % larger files. Palette (PNG/TIFF/GIF) quantisation keeps alpha 0/255 exact.

```python
# app/imaging/ops.py  — pure functions Image → Image
def parse_color(s) -> tuple[int,int,int,int]     # "#rgb", "#rrggbb", "#rrggbbaa", css names, "transparent"
def parse_ratio(r) -> float                       # "16:9", "4/5", "1.5", 1.5
def rotate(img, angle, expand=True, fill="#ffffff")
def flip(img, direction)
def crop(img, x, y, width, height, unit="px")
def crop_aspect(img, ratio, anchor="center", faces=None)
def trim(img, tolerance=12, padding=0)
def resize(img, mode, *, width=None, height=None, scale=None, size=None, megapixels=None,
           unit="px", dpi=300, upscale=True, resample="lanczos", background="#ffffff") -> Image
def pad(img, top=0, right=0, bottom=0, left=0, color="#ffffff")
def adjust(img, brightness=1.0, contrast=1.0, saturation=1.0, sharpness=1.0, grayscale=False, auto_contrast=False)
def smart_box(img, w, h) -> tuple[int,int]        # best top-left for a w×h window (saliency/entropy)
def to_px(value, unit, dpi) -> int
```

```python
# app/imaging/compress.py
def encode_to_target(img, fmt, *, max_bytes=None, min_bytes=None, **save_kwargs) -> tuple[bytes, dict]
# lossy (jpeg/webp/avif/heic): binary-search quality in [5, 95] from `quality` downwards (≤ 8 encodes; mid-then-floor
#   probe for hopeless targets); if q=5 is still too big, downscale so a mid quality fits and search again
#   (max 6 rounds, never below 16 px).
# outputs above 12 MP search on a ≤ 3 MP proxy whose sizes (calibrated by the real encodes) predict the full-size
#   result: ≤ 4 full-size encodes per output size, encodes that cannot fit are skipped, ≤ 40 encodes per job.
# png/gif/bmp/tiff: optimize → palette quantize (256→16 colours) → downscale.
# returns info {"quality", "scale", "iterations", "met": bool, "warning": str|None, "width", "height", "size_bytes"}.
# min_bytes: raise quality up to 100; if still below, warning (never pad with junk bytes).
```

```python
# app/imaging/info.py
def describe(loaded: Loaded, *, name: str, size_bytes: int, full_exif=False) -> dict   # FileInfo minus id
```

```python
# app/imaging/pipeline.py
@dataclass
class Ctx: image; dpi; exif; icc_profile; source_format; warnings: list[str]; meta: dict; cache_key: str;
           max_pixels: int; alive: Callable[[], bool] | None
def run(loaded, ops, *, cache_key="", max_pixels=None, alive=None, animation=None, on_progress=None, …) -> Ctx
def render(ctx, output: OutputSpec) -> tuple[bytes, str, dict]   # (data, fmt, {"width","height","dpi","warnings","encode"})
def passport_guides(ctx) -> dict | None                          # the preview's "passport" object
def plan(size, ops, output, *, source_format="jpeg", dpi=None, decode=True, frames=1) -> Plan   # no pixels touched
def forget(cache_key) ; def clear_caches()
```
`format: "original"` → the source format if it is an output format, else JPEG (alpha → PNG).
Analysis memoisation: `passport.analyze` results (faces, pupils, a ≤ 1600 px person mask; LRU of 8, ≤ 128 MiB) and
full-resolution float16 person masks for the `background` op (LRU of 3, ≤ 256 MiB) are keyed on
`<upload id>|<kind>|<json of the preceding ops>`, so slider tweaks don't re-run ONNX and an edited image never reuses
a stale analysis. Delete/expiry/wipe drop them (`forget`); a job finishing after its upload was deleted leaves nothing cached.

---

## 5. Passport / ID photos

### 5.1 Preset model (`app/imaging/presets.py`)
```python
@dataclass(frozen=True)
class PassportPreset:
    id: str                    # "us", "in", "uk", "schengen", …
    name: str                  # "United States — passport & visa (2×2 in)"
    country: str               # ISO-3166 alpha-2 or "EU"
    width_mm: float; height_mm: float
    dpi: int = 300             # print resolution
    pixel_width: int | None = None; pixel_height: int | None = None   # digital spec (e.g. US 600×600); overrides mm×dpi
    head_min: float = 0.0; head_max: float = 0.0     # chin→crown height as fraction of photo height
    eye_min: float | None = None; eye_max: float | None = None   # eye line, fraction of height measured from BOTTOM
    top_min: float | None = None; top_max: float | None = None   # crown→top margin, fraction of height
    background: str = "#ffffff"
    notes: str = ""
    source: str = ""           # URL of the official spec
    background_edit_allowed: bool = True   # False → the authority rejects digitally altered backgrounds
    crown_includes_hair: bool = True       # False → head height is chin → top of the skull (hair excluded)
    def output_size(self) -> tuple[int, int] ; def to_dict(self) -> dict   # to_dict adds width_px/height_px
```
**Shipped presets (20 + `custom`)**, each with its official source URL: `us` (passport & visa 2×2 in), `us-dv`
(Diversity Visa 600×600 px), `au`, `ca`, `ca-digital`, `ca-visa`, `cn` (visa), `fr`, `in` (passport, 630×810 px),
`in-visa`, `in-oci`, `jp`, `my`, `nl`, `nz-visa`, `schengen`, `sg`, `za`, `kr`, `uk`. Where a spec gives a range the
range is kept.
- `background_edit_allowed=false`: `ca`, `ca-digital`, `ca-visa`, `in`, `kr`, `sg`, `uk`.
- `crown_includes_hair=false` (head measured to the skull top): `ca`, `ca-digital`, `ca-visa`, `fr`, `kr`, `schengen`, `uk`.
A test ties each flag to its preset's notes. Countries without a preset yet are listed in §11.

### 5.2 Face geometry
1. `face.detect(img)` → YuNet on a ≤1024 px copy; boxes and 5 landmarks scaled back. Faces under 48 px at detection
   scale are re-detected on a crop (`refine_small`). Fallback: Haar cascade (no landmarks).
2. Pupils are refined per face (`refine_eyes`); **roll** = angle of the pupil line (accurate to ~0.2° up to ±15°).
   When the pupils can't be measured, a ≥ 5° tilt of the raw landmarks gives a coarser warning.
3. **Chin** ≈ the unclamped detector box bottom; when the box is cut off at the image bottom, the chin is extrapolated
   from the mouth (0.85 eye→mouth distances below it) and a warning is given. **Eye line** = pupil midpoint
   (fallback: box top + 0.38·h).
4. **Crown (hair top)**: if segmentation is available, the topmost row where the person mask (> 0.5) covers ≥ 15 % of
   the face-box column span, searching upward from the eye line; if the hair runs into the top edge, the crown is
   estimated above the image (warning). Without a mask: eye_y − (chin_y − eye_y)·1.0 − 0.10·face_h.
5. **Skull top** (presets with `crown_includes_hair=false`): eye line − 1.03 × (eye→chin) (adult anthropometric norm),
   pulled towards the visible head top when hair is short and never above it.
6. Head height `H = chin − crown` (crown per the preset's definition). All geometry is in levelled coordinates.

### 5.3 Framing algorithm (`passport.make(img, preset, …, level=True, analysis=None)`)
1. Output size `OW×OH` = pixel spec or `round(mm/25.4·dpi)`.
2. Target head height `h* = OH · mean(head_min, head_max) · zoom`; scale `s = h*/H`.
3. Vertical placement: if eye range present → eye line at `OH·(1 − mean(eye_min, eye_max))` from top, lowered within
   the range if that would crop the crown; elif top range → crown at `OH·mean(top_min, top_max)`; else crown at
   `(OH − h*)·0.45`. Then add `offset_y·OH`.
4. Horizontal: centre on the midpoint between the eyes (face-box centre fallback) + `offset_x·OW`.
5. **Levelling**: with `level` and 1° ≤ |roll| ≤ 15°, the frame is rotated about the eye midpoint inside the same
   single resample; |roll| > 15° → no rotation + warning; with `level=false`, |roll| > 8° warns.
6. Map the output frame back to source coords and resample once (Lanczos); areas outside the source are filled with
   the background colour. With a replaced background, "frame beyond the photo" is only warned about when the person is
   actually cut at that edge; hair cut at the left/right edge is warned about with the side named.
7. If `replace_background` and segmentation is available: composite the subject over `background` (mask from the
   **source**, feathered ~1.5 px, light edge decontamination). Otherwise keep the original background. For presets
   with `background_edit_allowed=false` it still replaces (the user asked explicitly) and warns:
   `"<Country> rules don't allow digitally altered backgrounds — photograph against a plain light wall instead."`
8. Return `(image, info)`; info keys are stable: `preset, width, height, dpi, face_found, head_ratio, eye_from_bottom,
   lines {crown, chin, eye} (output px), bands, background_edit_allowed, crown_includes_hair, roll_deg, levelled_deg,
   face_box_out, source_frame, warnings`. `levelled_deg` is applied in 0.1° steps (exactly the rotation used);
   `source_frame` = `{width, height, center, size, angle}` places the output frame on the input image (both the face
   and the no-face branch; see §3 `passport.source`). Without a face, offsets move the subject right/down by
   offset × frame size, like the face branch. The pipeline sets `dpi=(preset.dpi, preset.dpi)`. Warnings: no face (centred crop) or face
   detection unavailable, multiple faces, head ratio out of spec after clamping, frame exceeds source, low resolution,
   tilt, cut hair/chin, background rule.
9. Analysis is memoised by the pipeline (§4), not inside `passport`.

### 5.4 Print sheet (`passport.sheet(img, …, dpi=None, src_dpi=None)`)
Papers: `4x6` 101.6×152.4, `5x7` 127×177.8, `a4` 210×297, `letter` 215.9×279.4, `a5` 148×210, `10x15` 100×150, `13x18` 130×180 mm.
- Photo size = `photo_*_mm` (one side + image aspect is enough), else image px at the pipeline dpi (fallback 300).
  Photos under 5 mm per side → 422.
- `dpi=None` → the pipeline dpi (e.g. a 400-dpi preset) clamped to 300–600, else 300; pipeline dpi is set to it.
- Choose the orientation that fits more copies (`auto`; portrait wins ties), white paper, grid centred, 1 px mid-grey
  cut lines through the gaps (or along shared edges when a gap is 0) plus the outer edges.
- `copies=0` = as many as fit, at most 200; `copies` > fit → warning and clamp. Rows are pasted as strips, so the cost
  grows with rows, not copies.
- **Spacing is a preference, not a hard rule.** Let `m0 = 0 if paper.borderless else margin_mm` (photo papers 4×6, 5×7,
  10×15, 13×18 are borderless lab prints; A4/A5/Letter keep their margin for home printers). Max count = best over
  orientations with (gap 0, margin m0). Try (gap_mm, margin_mm) → (gap_mm, m0) → (0, m0); use the first reaching the
  max. Then each axis gets as much of `gap_mm` as its spare paper allows (`info.gap_x_mm`, `info.gap_y_mm`).
- Expected: 2×2 in on 4×6 → 6 (portrait 2×3, gaps 0); 35×45 mm on 4×6 → 8 (landscape 4×2, gap 2, margin 0);
  35×45 mm on A4 → 32 (landscape 8×4, margin 4, gap 1.29 mm between columns and 2 mm between rows).

---

## 6. Background, matting, upscaling and detection

**People — `app/imaging/segment.py`** (U²-Net human segmentation; passport backgrounds, "person" removal):
```python
def available() -> bool
def person_mask(img, *, size=None) -> np.ndarray   # float32 HxW in [0,1]; U²-Net human seg @320×320, ImageNet mean/std,
                                                   # output[0] min-max normalised, resized back with bilinear, slight blur
def composite(img, mask, color, *, feather=1.5, decontaminate=True) -> Image   # color "transparent" → RGBA
def remove_background(img) -> Image         # RGBA
def replace_background(img, color) -> Image
```
Sessions: `onnxruntime.InferenceSession(path, providers=["CPUExecutionProvider"])`, `intra_op_num_threads` = min(4, cpus),
created lazily, thread-safe. The background op works on images up to 50 MP (≈ 33 B/px peak).

- **Any subject — `app/imaging/matting.py`**: BiRefNet lite at 1024×1024 (`HEYP_MATTING_MODEL=fp32|int8`),
  `object_mask(img, size=…)`, `remove_background(img)`; the `background` op picks it with `engine: object` (`auto`
  prefers it when installed, `person` forces U²-Net).
- **Upscaling — `app/imaging/upscale.py`**: Real-ESRGAN general x4v3 (`strong`) and its 50 % blend with the denoising
  weights (`balanced`), run in tiles of at most 256 px with 16 px of context (`HEYP_AI_THREADS` threads; ~11 s per input
  megapixel on 4 cores); 2× halves each 4× tile with an area filter, so no 4× frame is ever held; alpha is resized
  separately and fully transparent pixels take the nearest visible colour first, so hidden colour never fringes the
  edge. `check_limits` refuses inputs above `HEYP_UPSCALE_MAX_INPUT_MP` or outputs above the pixel
  limit before any work.
- **Faces — `app/imaging/face.py`** (YuNet, §5.2) and **plates — `app/imaging/plates.py`** (YOLOv9-t, letterboxed
  640 px tiles with overlap, merged): `GET /api/detect/{id}` and the `img.blur` tool turn them into suggested
  regions; `app/imaging/blur.py` applies blur, pixelate or a solid fill per region (`HEYP_DETECT_CONCURRENCY` runs at
  once). Models load lazily through `app/imaging/ortsession.py` (one session per model, CPU provider; freed heap pages
  go back to the OS after a run).

---

## 7. UI (static/, no build step)

**Look — editorial ("Broadsheet")**: a newspaper front page for photos. Nameplate `heyp :)`, a dateline with one
factual line from `/api/config` (how long files are kept), the "heyp · heypdf" switch and today's date, a numbered
section rail whose first nine numbers are the `1–9` shortcuts, the picture as "Fig. 1" with a caption, printer's crop
marks on the crop overlay, a red square marking edited tools and edit chips. Fonts are self-hosted (SIL OFL,
`static/fonts/`): Fraunces (display), Libre Franklin (UI), IBM Plex Mono (figures). Colour tokens on `:root`
(newsprint `--paper`, `--ink`, editorial red `--accent`, ochre `--warn`, green `--success`) with a dark set via
`prefers-color-scheme` + a manual toggle (the only browser-storage key, `heyp-theme`, in try/catch). Works from 360 px
wide (on phones the picture stays in view while the panel scrolls under it).

**Sections (rail order)**: Convert · Resize · Crop · Passport · Compress · Edit · Background · Upscale · Watermark ·
Blur · Photo editor · Meme · Make GIF · Web page (HTML → image) · Info.

- **Intake**: drag & drop, file picker, paste. Files are pre-checked against the server's `limits` (size per file,
  files and bytes per request) and uploaded in request-sized chunks with progress (`?accept=image`; anything else is
  refused with a reason); thumbnails come from `/api/files/{id}/preview?max=240`.
- **Edits model**: global settings; ops are built in a fixed order (`blur_regions → rotate/flip → crop → background →
  passport → resize → adjust → filter → upscale → frame → meme → overlay → watermark → sheet`). Crops, blur boxes and editor layers are
  stored **per file** and travel in `ops_by_id`, so every export is one request. Active edits appear as removable
  chips. Passport active ⇒ crop/resize are disabled (it defines the frame).
- **Live preview**: debounced `POST /api/preview`; stale responses discarded (sequence + AbortController). A 503 is
  retried after its `Retry-After`; 404 removes the expired file.
- **Export (pinned at the foot of the panel)**: format, quality, "Keep metadata" and "Keep location (GPS)" (both off
  by default), Download. Small exports download directly from `/api/process`; slow ones (several photos, upscaling,
  background removal) run as the `img.process` job with progress and a result card. After an export, **"Continue
  with"** offers the heyp sections that take the result; when `/api/config` names a sibling it also lists **"Open
  heypdf"** — a plain link (new tab) to the other site: no file and no id goes with it, and the menu says so.
- **Job tools** (Make GIF, HTML → image, Compress in the original format, Upscale/Background/Blur on many photos) show
  a job card: queued position, progress from the event stream (polling as a fallback), cancel, result files with
  downloads, "Add to photos" and "Continue with".
- **Copy honesty**: every sentence about where files go or how long they stay is derived from `/api/config`
  (`static/js/config.js`); when the config can't be loaded no claim is printed. There is no "local only", "files stay
  on this machine" or "edition" wording anywhere (`static/tests/review-fixes.test.mjs` checks the page and styles).
- **Removing** a photo waits 8 s with an Undo toast before the server copy is deleted; leaving the page with photos
  asks first, and closing the tab asks the server to delete the page's uploads and results (`keepalive`).
- **Keyboard**: `1–9` sections, hold `Space` = original, `R` / `Shift R` rotate, `Ctrl/Cmd+S` download,
  `Ctrl/Cmd+O` add, `[`/`]` previous/next photo, `Del` remove (with Undo), `Esc` revert crop, `?` shortcuts dialog.
- **Accessibility**: real buttons/labels, visible focus, `aria-live` status, `aria-busy` canvas, reduced motion
  respected; the "Continue with" menu is a `<details>` disclosure.

---

## 8. Job tools (`app/core/registry.py`, `app/core/jobs.py`, `app/tools/`)

Work that can take longer than a proxy allows for one request runs as a **job**. Each tool module registers its
tools at import (`registry.register(Tool(...))`); `GET /api/tools` lists them, `POST /api/jobs` runs them.

```python
@dataclass(frozen=True)
class Tool:
    id: str                  # "img.<name>"
    title: str; summary: str; category: str
    inputs: tuple[str, ...]  # kinds ("image", "html") or formats ("png", …)
    options: type[BaseModel] # pydantic, extra="forbid"; UI hints in json_schema_extra={"ui": {...}}
    run: Callable[[JobContext, BaseModel], None]
    min_files: int = 1; max_files: int = 1
    engine: "app" | "renderer" = "app"; lane: "light" | "heavy" | "render" = "light"
    timeout_s: int = 120; limits: {"max_mb", "max_files", "memory_mb"}; outputs: str
    check: Callable[[], tuple[bool, str]] | None    # e.g. "the model file is installed"
    validate: Callable[[stored_files, options], None] | None   # submit-time check (422 before queueing)
```

| tool | lane | what |
|---|---|---|
| `img.process` | heavy | the export pipeline as a job (one file per photo + ZIP, or one combined PDF), up to 500 photos / 30 min |
| `img.make_gif` | heavy | animated GIF or WebP from 1–300 frames: order, delay, loop, size, fit, edits applied per frame |
| `img.compress` | light | compress in the original format (lossy re-encode, PNG palettes via pngquant, animated GIF via gifsicle, SVG via scour) |
| `img.upscale` | heavy | Real-ESRGAN 2×/4× (balanced or strong), tiled, inputs up to `HEYP_UPSCALE_MAX_INPUT_MP` |
| `img.remove_background` | heavy | BiRefNet (any subject) or U²-Net (people): transparent or a colour |
| `img.blur` | light | detect faces/plates and blur, pixelate or cover them plus any marked areas |
| `img.html_to_image` | render | a web address or an uploaded HTML file → JPG/PNG through the renderer (§9) |

**Runner**: `JobManager.submit` validates the tool, its availability (engine health checks are cached 10 s), input
count, kinds and size limits, and the options; in hosted mode it charges the per-client limits (§11). Each lane has
`HEYP_LANE_<LANE>` workers and a queue of `HEYP_QUEUE_MAX` (full → 503 + `Retry-After`). Heavy-lane tools reserve
memory from the shared budget first. Inputs are pinned while the job runs; outputs go to the store as
`source: "result"` files that inherit the inputs' earliest hard deadline (several outputs also get a ZIP). Every job
has a deadline: `check_cancel()` raises at it, and a watchdog fails a job that ignores it 5 s later and replaces its
worker thread. Errors map to statuses (`ValueError` → 422, anything with a `status` → that status, otherwise 500
with a generic message; tracebacks are logged without messages or file names). Options holding a capture address
are scrubbed from the job record when the job ends. Finished records expire with their files.

---

## 9. Services: renderer and egress (`services/`)

| image | what | network | interface |
|---|---|---|---|
| `heyp-renderer` | Playwright + chromium-headless-shell, Chromium's own sandbox on (seccomp profile `deploy/seccomp/chromium-userns.json`) | `render-sbx` only (internal, isolated gateway): its only peer is egress | Unix socket `/run/renderer/r.sock` on a shared volume — `POST /v1/render` (URL or HTML → PNG/JPEG + `X-Render-Info`), `GET /healthz` |
| `heyp-egress` | stdlib-only policy proxy: resolves the name once, checks every address, connects to the vetted address | `render-sbx` + `egress-wan` (the only route out) | :3128 deny-all, :3129 control (`X-Egress-Secret`), :3130+ one port per render slot |

The app has **no IP route** to the renderer (only the socket), and the renderer has no route to the app, the host or
the internet except through egress. Egress refuses private, loopback, link-local, metadata, CGNAT, NAT64, 6to4, ULA
and other reserved ranges, ports other than 80/443, and the server's own public addresses (`HEYP_EGRESS_DENY_CIDRS`,
required in production); because it connects to the address it checked, DNS rebinding can't switch the target.
Every request of the page goes through it (Chromium is forced onto its slot's proxy port, WebSockets are mocked,
service workers and downloads blocked). The renderer caps navigations, requests, page height, pixels, output size
and time. `app/clients/renderer.py` maps its error codes to clear messages; `app/tools/render.py` adds friendly early
URL checks and the per-client and per-site render limits. `services/e2e/` is the SSRF test bed (hostile targets:
internal service, cloud metadata v4/v6, DNS rebinding, UDP sink) run in enforce, sandbox, proxy-only, raw-proxy and
positive-control modes. Neither service logs URLs or hosts.

---

## 10. Security model

- **Outer boundary — containers**: non-root uids, read-only root filesystems, `/tmp` tmpfs the only writable path,
  `cap_drop: ALL`, `no-new-privileges`, pids/memory/CPU limits, digest-pinned base images, hash-locked dependencies,
  sha256-pinned models. The app publishes on 127.0.0.1 only.
- **Untrusted files**: kinds are decided from the bytes, never the name; header probes enforce pixel and frame limits
  before decoding; camera RAW (LibRaw) and SVG (resvg) are decoded in `run_isolated` children (`app/core/sandbox.py`)
  with RLIMIT_AS/CPU/FSIZE, no core dumps, a wall-clock timeout tied to the job's deadline, and `app/core/confine.py`:
  secret-looking variables removed (before the forkserver starts, and again in the child), the app process
  non-dumpable, seccomp denying `socket()`/`ptrace`/`process_vm_*`/io_uring, and Landlock hiding the file store except
  the call's own inputs and working directory. SVGs are also stripped of scripts, external references and fonts.
- **Requests**: cross-origin guard, body caps, work budgets, bounded queues, dotfile 404, CSP `'self'` with sandboxed
  downloads, no cookies, no third-party scripts or fonts.
- **Web capture**: §9.
- **Abuse** (hosted): §11.
- **Secrets**: the app needs none; the renderer and egress share `HEYP_EGRESS_SECRET` (control API). Deploy secrets
  live only in GitHub's `production` environment.
- **Supply chain**: every workflow action pinned to a commit SHA, least-privilege tokens, zizmor and actionlint in CI,
  CodeQL, Grype gate on fixable critical vulnerabilities, Dependabot with a cool-off, SLSA provenance + SBOM
  attestations verified before every deploy, the server only deploys commits on `main` (or `v*` tags).

---

## 11. Hosted mode, legal pages and abuse limits

`HEYP_MODE=hosted` turns the app into a public site; it refuses to start without `HEYP_OPERATOR_NAME`,
`HEYP_OPERATOR_EMAIL` and `HEYP_PUBLIC_URL`, or with a hard TTL of 0.

- **Retention**: hard TTL (default 240 min after upload) on top of the idle TTL; results inherit their inputs'
  deadline; `Cache-Control: no-store` on every API response; no app access log (`heyp-serve` adds `--no-access-log`).
- **Legal pages** (`app/legal.py`): `/privacy`, `/terms` and `/credits` are rendered from `legal/privacy.md`,
  `legal/terms.md`, `legal/glance.md` and `CREDITS.md` — a Mustache subset filled with escaped configuration values
  (operator, grievance officer, jurisdiction, TTLs, log retention, hosting provider, CDN, server location, the
  running version and commit), then a small Markdown subset, then `app/templates/legal.html`. Sections appear only
  when true for the deployment (the web-capture section only when the renderer runs; the CDN wherever traffic passes
  through it). Operator `VERIFY:` notes are HTML comments and never ship. In local mode `/privacy` and `/terms` show
  `legal/local.md`, a short notice. The production values: operator Usham Roy, contact and grievance officer
  support@edgr.in, jurisdiction India, Cloudflare as the CDN. `tests/test_legal.py` checks every page variant, the
  browser-storage keys the UI writes, cookies, the Apache log format and retention, and the CDN disclosure.
- **Abuse limits** (`app/core/limits.py`, on in hosted mode): token buckets per client — upload bytes/hour, jobs/minute,
  web-page captures/minute and per destination site, active jobs. The client key is SHA-256(daily random salt ‖ IP),
  kept in memory only; the IP comes from `X-Forwarded-For` only when the peer is in `HEYP_TRUSTED_PROXIES` (walked
  right to left up to the first untrusted hop). Refusals are 429 with `Retry-After`.
- **The sibling**: `HEYP_SIBLING_URL` (production: https://heypdf.edgr.in) drives the "heyp · heypdf" switch on the
  page and the legal pages and the "Open heypdf" entry; `/pdf` and `/pdf/<path>` answer 308 to the same path there.
  It is a plain link — the two apps share no storage, ids or code.

---

## 12. Docker images

- `Dockerfile` (app, `ghcr.io/uroy80/heyp`): `pybase` (python:3.12-slim-trixie pinned by digest) → `models`
  (`fetch_models.py`, size + sha256 pinned; optional `HEYP_MODELS_MIRROR` build arg) → `base` (gifsicle, pngquant,
  libfribidi; `pip install --require-hashes --no-deps -r requirements.lock && pip check`; the build loads the models)
  → `dev` (+pytest/httpx, `HEYP_REQUIRE_MODELS=1`; source bind-mounted) | `runtime` (default, last: non-root uid
  10001, pip removed, `MALLOC_ARENA_MAX=2`, `heyp-serve` = uvicorn with `--forwarded-allow-ips` from
  `HEYP_TRUSTED_PROXIES` (never `*`) and no access log in hosted mode, HEALTHCHECK on `/api/health`, OCI labels with
  the version and revision).
- `services/renderer/Dockerfile` (`heyp-renderer`) and `services/egress/Dockerfile` (`heyp-egress`): `base` → `test`
  (their own suites with the same engines and limits) | `runtime` (default).
- `.dockerignore` is an allowlist (`app/`, `static/` minus `static/tests/`, `legal/`, `CREDITS.md`, `LICENSE`, the
  fetch script, the locks) that also drops dotfiles, backups and bytecode.
- `docker-compose.yml` (local): `heyp` on `${HEYP_BIND:-127.0.0.1}:${HEYP_PORT:-8080}`, `mem_limit 4g`, `pids_limit`,
  read-only, `/tmp` tmpfs 1g, no capabilities; `renderer` + `egress` behind the `render` profile.
  `deploy/compose.prod.yml` (production) runs the published images with the same hardening, hosted mode and the
  settings of §2 (§14).
- `scripts/smoke.sh` builds/runs the runtime image hardened like compose and checks health, config, the legal pages,
  the `/pdf` redirect, the tool registry, a compress job, HEIC → JPEG, resize, the pixel guard, faces, background,
  passport + sheet, the cross-origin and dotfile guards and delete.

---

## 13. Testing

- **Python** (`docker run --rm --network none -v "$PWD":/app -w /app heyp:dev pytest -q -m "not services"`): unit
  tests per module with synthetic images (the only binary fixtures are `tests/fixtures/astronaut.png` and the small
  model fixtures in `tests/fixtures/ai/`): decoding and colour (HEIC orientation applied once, P3 ICC and nclx →
  sRGB, RAW/PSD/SVG, animations and frame bombs), every op and output format, the target-size encoder, presets and
  passport framing against measured ground truth, print sheets, the models (face, segmentation, matting, upscaling,
  plates — with `HEYP_REQUIRE_MODELS=1` a missing model fails the suite), the registry and job runner (lanes, queues,
  deadlines, watchdog, SSE, memory admission, limits), the sandbox and its confinement, the HTTP surface (uploads,
  guards, config, tools, redirects, hosted mode), the legal pages, and the delivery files (compose, Dockerfile,
  locks, workflows, deploy scripts, Apache configuration).
- **Services**: each image's `test` stage (`.github/scripts/service-tests.sh renderer|egress`) and the e2e SSRF test
  bed (`services/e2e/run.sh`); `pytest -m services` runs the app's renderer tests against a real renderer socket.
- **UI**: `node --test static/tests/*.test.mjs` (state and op builders, uploads and limits, job client and SSE, copy
  honesty, "Continue with" and the sibling link, retries).
- **Deploy**: `.github/scripts/validate-deploy.sh` (compose config, `apachectl -t` and a functional check of the
  Apache vhost in a throwaway `httpd:2.4` container), `scripts/smoke.sh`, `.github/scripts/stack-smoke.sh` (the
  production compose file with locally built images, checked from outside: health, config, legal pages, redirects,
  a capture-free privacy check, network isolation).

---

## 14. CI/CD and deployment

- **CI** (`.github/workflows/ci.yml`, every PR and push): ruff, shellcheck, actionlint, deploy configs; zizmor; model
  sources probe; Python tests in the dev image; UI syntax + unit tests; service test stages; the three images built,
  smoke-tested hardened and scanned (Grype → SARIF, gate on fixable criticals); the full-stack compose smoke; CodeQL
  (Python, JavaScript, Actions); dependency review on PRs; one required check, **CI OK**.
- **Release** (`release.yml`, push to `main`): each image built natively on amd64 and arm64 runners, pushed by digest,
  merged into one index tagged `sha-<commit>` and `main`, with SLSA provenance and SBOM attestations; then
  `deploy.yml` when `DEPLOY_ENABLED=true`. A `v*` tag promotes the same digests to `X.Y.Z`, `X.Y`, `X`, `latest` and
  creates the GitHub Release.
- **Deploy** (`deploy.yml`, environment `production`): verify the attestations of `heyp`, `heyp-renderer` and
  `heyp-egress` for the commit, `ssh deploy@vps deploy <commit>` (forced command `deploy/heyp-deploy`: commit must be
  on `main` or a `v*` tag; compose files from that commit; pull, `up --wait`, revision labels checked; rollback on
  failure), then public checks (health, pages, no cookies, no `/cdn-cgi/` scripts). `PROD_ENV` lines (`HEYP_*` only,
  network keys refused) replace the server's `ci.env` on every deploy.
- **Relock** (`relock.yml`): regenerates the hash locks on Dependabot's pip PRs.
- **Target**: the Hostinger VPS behind Apache + Cloudflare, app on 127.0.0.1:8180 next to heypdf on :8181 —
  `deploy/DEPLOY.md` is the runbook (`bootstrap-vps.sh`, `heyp.env.example`, `apache/`, the Cloudflare settings).

---

## 15. Roadmap

Saved workflows (chains of tools) · collage & contact sheets · document scan mode (perspective deskew) · signature
cleanup (for exam forms) · PWA/offline install · CLI wrapper (`docker run heyp convert …`) · optional basic-auth for
LAN exposure · HDR (PQ/HLG) tone mapping · an int8 U²-Net option once a portrait corpus confirms edge quality · more
presets: US green card, New Zealand passport, UAE, Saudi Arabia, Pakistan, Bangladesh, Nepal, Brazil, Mexico,
Russia, Philippines, Nigeria.
