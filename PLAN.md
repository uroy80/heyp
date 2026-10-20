# heyp — plan & build spec (v1.1)

> **heyp** is a self-hosted, single-container photo toolbox: HEIC → JPG/PNG conversion and every
> everyday photo job (resize, resolution/DPI, crop & auto-crop, compress to a target KB,
> passport/ID photos with print sheets, background replace, metadata) behind one quiet UI.
> Everything runs locally in Docker; no file ever leaves the machine.

This file is both the product plan and the **contract** the implementation follows. Module
boundaries, function signatures and the HTTP API below are binding — if code and this file
disagree, fix one of them deliberately.

**v1.1** (this revision) folds in the fix-round contract (C1–C7): per-file ops (`ops_by_id`), preview
`encode`/`passport` objects, preset rule flags, head levelling, sheet dpi from the pipeline, service
hardening (memory budgets, cross-origin guard, docs gating, dotfiles, body and work limits, localhost
bind) and `limits` in `/api/health`. The UI was restyled to the editorial design (§7).

---

## 1. Goals / non-goals

**Goals**
- Open anything a phone or camera produces: HEIC/HEIF (iPhone), JPEG, PNG, WebP, AVIF, TIFF, BMP, GIF (first frame), ICO, JPEG 2000.
- Convert to JPEG, PNG, WebP, AVIF, HEIC, TIFF, BMP, GIF, PDF (multi-image → one PDF).
- Batch everything (many files in → ZIP or one PDF out), including per-file crops.
- Correct by default: EXIF orientation applied once, wide-gamut (ICC or HEIC/AVIF `nclx`) → sRGB, alpha flattened onto a chosen colour for formats without alpha, DPI written where the format supports it.
- Passport/visa/ID photos for common countries: automatic face-aware framing to official head-size rules, head levelling, white/blue background replacement, print sheets (4×6 in, A4, …) with cut lines.
- Quiet, fast, keyboard-friendly UI; works offline (no CDN; fonts are self-hosted).
- One command: `docker compose up -d --build` → http://localhost:8080 (loopback only by default).

**Non-goals (v1.x)**: accounts, cloud storage, persistence beyond a TTL'd temp store, RAW (CR2/NEF/DNG) decoding,
video/Live Photo, AI upscaling, OCR, HDR tone mapping. (See §11 roadmap.)

---

## 2. Architecture

```
Browser (vanilla HTML/CSS/JS, served from /)
   │  fetch JSON / multipart (same origin only for POST/DELETE)
   ▼
FastAPI  app/main.py  security headers · cross-origin guard · dotfile 404 · body caps · docs gating
         app/api.py   /api routes · job admission (slots + memory budget + bounded queue) · work budget
         app/store.py FileStore (/tmp/heyp, TTL, uuid ids) · decoded-image LRU (items + bytes)
   │
   ▼
Pipeline (app/imaging/pipeline.py): plan → load → [ops…] → encode
   ├── io.py        open/normalize/save, colour mgmt, metadata
   ├── ops.py       rotate, flip, crop, crop_aspect, trim, resize, pad, adjust
   ├── compress.py  encode to a target size (KB)
   ├── info.py      metadata/EXIF summary
   ├── face.py      YuNet face detection (OpenCV) + Haar fallback, pupil refinement
   ├── segment.py   U²-Net human segmentation (onnxruntime) → background remove/replace
   ├── presets.py   passport presets (official sources, rule flags) and papers
   └── passport.py  face-aware framing, levelling, print sheets
```

- **Stack**: Python 3.12, FastAPI 0.143, Pillow 12.3 (native AVIF), pillow-heif 1.8 (libheif 1.23, HEIC read+write),
  OpenCV-headless 5.0 (YuNet), onnxruntime 1.31, numpy 2.5, plain `uvicorn` (no `[standard]` extras in the image).
  Every Python dependency is pinned with hashes in `requirements.lock` / `requirements-dev.lock` (`scripts/lock.sh`).
  Frontend: no framework, no build step.
- **Models** (downloaded at image build by `scripts/fetch_models.py` into `$HEYP_MODELS_DIR=/opt/heyp/models`, each
  checked against a pinned size + sha256; any mismatch fails the build):
  `face_detection_yunet_2023mar.onnx` (230 KB), `u2net_human_seg.onnx` (176 MB). At runtime missing models
  degrade gracefully (feature flag false, Haar fallback for faces, passport works without bg replacement).
- **Concurrency**: CPU work runs in sync (`def`) endpoints → Starlette threadpool. `app.state.jobs` (`api.JobSlots`)
  admits heavy work first-come-first-served: at most `HEYP_MAX_JOBS` at once, their planned peak memory
  (`pipeline.plan`) within `HEYP_JOB_MEMORY_MB`, and a bounded wait queue (`max(8, 24 − max_jobs)`); a request
  that can't queue gets **503 + `Retry-After: 5`** at once. Cheap endpoints (`health`, `formats`, `presets`,
  `DELETE`) are `async` and never queue behind jobs. ONNX/YuNet sessions are created lazily once and guarded by a lock.
- **Privacy**: uploads live in `HEYP_TMP_DIR` under a random uuid4-hex id, deleted after `HEYP_TTL_MINUTES`
  of inactivity, on `DELETE`, and on restart. Logs carry uvicorn's request lines (client address, method,
  path with opaque ids, status) — never file names or image data; `/api/health` polls are not logged.

### Directory layout
```
heyp/
├── Dockerfile               # targets: models, base, dev (tests), runtime (default, last)
├── docker-compose.yml       # loopback bind, mem_limit, read-only, tmpfs
├── requirements.txt / requirements-dev.txt  → requirements.lock / requirements-dev.lock (hashes)
├── scripts/fetch_models.py · lock.sh · smoke.sh
├── app/
│   ├── main.py              # FastAPI app factory, middleware, static mount, lifespan (cleanup loop)
│   ├── config.py            # Settings from env
│   ├── store.py             # FileStore + decoded-image LRU
│   ├── schemas.py           # pydantic request models (ops = discriminated union)
│   ├── api.py               # /api routes, job admission
│   └── imaging/{io,ops,compress,info,face,segment,presets,passport,pipeline}.py
├── static/{index.html, styles.css, app.js, favicon.svg, fonts/, js/, js/tools/}  (+ tests/: node unit tests, not shipped)
└── tests/                   # pytest; fixtures generate images incl. HEIC on the fly
```

### Configuration (env)
| var | default | meaning |
|---|---|---|
| `HEYP_BIND` | 127.0.0.1 | (compose) host address the port is published on; `0.0.0.0` exposes heyp to the LAN — it has **no authentication** |
| `HEYP_PORT` | 8080 | (compose) published port |
| `HEYP_MEM_LIMIT` | 4g | (compose) container memory limit (`mem_limit`) |
| `HEYP_TMP_SIZE` | 1g | (compose) size of the RAM-backed `/tmp` tmpfs (uploads, ZIP spools); full → 507 |
| `HEYP_MAX_UPLOAD_MB` | 100 | per-file upload limit → 413 |
| `HEYP_MAX_FILES` | 200 | files per upload request and ids per process request |
| `HEYP_MAX_PIXELS` | 50_000_000 | pixel limit for decoded and produced images (header-checked before decoding) → 413 |
| `HEYP_TTL_MINUTES` | 60 | inactivity TTL for uploads |
| `HEYP_TMP_DIR` | /tmp/heyp | upload store |
| `HEYP_MAX_JOBS` | max(2, min(4, CPUs)); compose: 2 | concurrent heavy jobs |
| `HEYP_JOB_MEMORY_MB` | 0 = 60 % of the container limit − cache | memory the running jobs may plan to use together |
| `HEYP_CACHE_ITEMS` | 6 | decoded images kept in memory (upper bound) |
| `HEYP_CACHE_MB` | 512 | decoded-image LRU budget in bytes of pixel data; larger images are not cached |
| `HEYP_ALLOW_CROSS_ORIGIN` | 0 | 1 disables the cross-origin guard (§3) |
| `HEYP_DOCS` | 0 | 1 mounts `/docs`, `/redoc`, `/openapi.json` |
| `HEYP_MODELS_DIR` | /opt/heyp/models | ONNX models |
| `HEYP_PREVIEW_MAX` | 1600 | max side of preview images |
| `HEYP_REQUIRE_MODELS` | 1 in the dev image | tests that need the models fail instead of skipping |

Invalid values (non-integers, below the minimum, flags other than 0/1/true/false/yes/no/on/off) fail at start-up.

---

## 3. HTTP API (all JSON unless noted; errors are `{"detail": "..."}`)

| method & path | purpose |
|---|---|
| `GET /api/health` | `{"status":"ok","version":"1.1.0","features":{"heic","avif","face","segmentation":bool},"limits":{"max_upload_mb","max_files","max_pixels"}}` — `max_pixels` is the stricter of the app and decoder limits (C7) |
| `GET /api/formats` | `{"input":["heic","heif","jpeg","png","webp","avif","tiff","bmp","gif","ico","jp2"],"output":["jpeg","png","webp","avif","heic","tiff","bmp","gif","pdf"]}` |
| `GET /api/presets/passport` | `{"presets":[Preset…], "papers":[{"id":"4x6","name":"4×6 in","width_mm":101.6,"height_mm":152.4,"borderless":true},…]}`; presets carry `width_px`/`height_px` and the C3 flags |
| `POST /api/files` | multipart, field `files` (repeatable). → `{"files":[FileInfo…],"errors":[{"name":str,"detail":str}…]}` (200 if ≥1 ok; if all fail the status is the most relevant of 507/503/413/415/400 and the body adds `detail`) |
| `GET /api/files/{id}` | `FileInfo` incl. `exif` (full readable dict) |
| `GET /api/files/{id}/preview?max=1600` | oriented, sRGB, downscaled **image/jpeg** (or **image/png** when the image has alpha). `Cache-Control: private, max-age=3600`. Only sides 240/480/960/1600 and `HEYP_PREVIEW_MAX` are cached on disk |
| `GET /api/files/{id}/faces` | `{"width":W,"height":H,"faces":[{"box":[x,y,w,h],"score":f,"landmarks":{"right_eye":[x,y],…}}]}` in oriented-image px; 503 when no detector is available |
| `DELETE /api/files/{id}` | 204 (also drops the decoded image and every analysis/mask cache of that upload) |
| `POST /api/preview` | body `ProcessRequest` with exactly one id. Runs the **full-resolution** pipeline and encode (so size is exact) → `PreviewResponse` below |
| `POST /api/process` | body `ProcessRequest`. One id → the file itself; several ids → `application/zip` (or one PDF if `output.format=="pdf"` and `output.combine_pdf`). Headers: `Content-Disposition` (RFC 5987 `filename*`), single file: `X-Heyp-Width`, `X-Heyp-Height`; `X-Heyp-Warnings` (URL-encoded JSON list; for ZIP/PDF entries read `"<file name>: <warning>"`, present only when there are warnings; capped at ~4 KB with a final "…and N more warnings") |

**Status codes**: 400 empty upload · 403 cross-origin state-changing request · 404 unknown/expired id or path ·
413 too large (bytes, pixels, body, ZIP size, or estimated work) · 415 undecodable/unsupported · 422 invalid ops/request ·
499 client disconnected during a batch · 503 busy (`Retry-After`) or feature unavailable · 507 server storage full ·
500 never for user input (500 responses still carry the security headers).
Ids are validated against `^[0-9a-f]{32}$` before touching the filesystem.

**Service guards (C6)**
- *Cross-origin guard*: unless `HEYP_ALLOW_CROSS_ORIGIN=1`, POST/PUT/PATCH/DELETE with `Sec-Fetch-Site: cross-site`,
  an opaque `Origin` (`null`), or an `Origin` whose host differs from `Host` (or `X-Forwarded-Host`) → 403
  `{"detail":"Cross-origin requests are not allowed"}`. Requests without `Origin` (curl, scripts) pass.
- *Docs*: `/docs`, `/redoc`, `/openapi.json` exist only with `HEYP_DOCS=1` (they are 404 otherwise).
- *Dotfiles*: any path with a segment starting with `.` (`/.DS_Store`, `/.env`) → 404, before static files.
- *Security headers* on every response: CSP `default-src 'self'` (+ `img-src data: blob:`), `nosniff`, `no-referrer`;
  static files `Cache-Control: no-cache`.
- *Body caps* before parsing (declared or chunked): multipart ≤ `max(256, HEYP_MAX_UPLOAD_MB) + 1` MB, any other
  body ≤ 2 MB → 413.
- *Pixel admission*: uploads are header-probed and refused above `HEYP_MAX_PIXELS` before decoding. Ops/encoders with
  a high memory cost per pixel have their own caps → 413: background 50 MP, adjust 100 MP, AVIF output 50 MP,
  lossless WebP 40 MP, GIF 100 MP. A combined PDF holds at most `HEYP_MAX_PIXELS` in total.
- *Work budget*: every item of a request is costed (`pipeline.plan`) before any work; more than ~60 min of estimated
  CPU → 413. Repeated ids are processed once. A ZIP is capped at min(2 GB, half the free space of the upload
  volume) → 413; a full volume → 507. ZIP/PDF batches stop between items when the client disconnects (499), and
  uploads in a running batch are pinned so they can't expire mid-way.

### FileInfo
```json
{"id":"…32 hex…","name":"IMG_0001.HEIC","format":"heic","mime":"image/heic","size_bytes":1834221,
 "width":4032,"height":3024,"mode":"RGB","has_alpha":false,"frames":1,"bit_depth":8,
 "dpi":[72,72] | null,"orientation":6,"icc":"Display P3" | null,
 "print_size_in":[13.44,10.08] | null,          // at the file's DPI, null if no DPI
 "camera":{"make":"Apple","model":"iPhone 15 Pro","lens":"…","datetime":"2026:09:01 10:22:01","iso":80,"exposure":"1/120","f_number":1.8,"focal_length_mm":6.8} | null,
 "gps":{"lat":12.97,"lon":77.59} | null,
 "exif":{"Make":"Apple", …}                     // only on GET /api/files/{id}; readable values, bytes dropped
}
```
`width`/`height` are **after** orientation is applied (what the user sees).

### ProcessRequest
```json
{
  "ids": ["…"],                    // 1…HEYP_MAX_FILES; duplicates are processed once (first position)
  "ops": [ Op, … ],                // ≤ 32, applied in order; the image is always EXIF-oriented first
  "ops_by_id": {"<id>": [ Op, … ]},// optional (C1): replaces `ops` entirely for that id; keys ⊆ ids (else 422)
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

### PreviewResponse (C2)
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
| `passport` | `preset: str`, `custom: CustomPreset?`, `background: str\|null = preset default`, `replace_background = true`, `face_index = 0`, `zoom = 1.0` (0.5–2), `offset_x = 0.0`, `offset_y = 0.0` (−1…1), `level = true` (C4) | §5 |
| `sheet` | `paper = "4x6"`, `orientation: auto\|portrait\|landscape = auto`, `photo_width_mm?`, `photo_height_mm?` (5–500), `dpi: int\|null = null` (72–1200; C5), `gap_mm = 2`, `margin_mm = 4`, `cut_lines = true`, `copies = 0` (0–200; 0 = as many as fit, at most 200) | tile current image onto paper (§5.4) |

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
def load(source: str | bytes | BinaryIO) -> Loaded
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
def run(loaded, ops, *, cache_key="", max_pixels=None, alive=None) -> Ctx
def render(ctx, output: OutputSpec) -> tuple[bytes, str, dict]   # (data, fmt, {"width","height","dpi","warnings","encode"})
def passport_guides(ctx) -> dict | None                          # the preview's "passport" object (C2)
def plan(size, ops, output, *, source_format="jpeg") -> Plan      # Plan(work s, peak_bytes, peak_pixels), no pixels touched
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
    background_edit_allowed: bool = True   # C3: False → the authority rejects digitally altered backgrounds
    crown_includes_hair: bool = True       # C3: False → head height is chin → top of the skull (hair excluded)
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
5. **Levelling (C4)**: with `level` and 1° ≤ |roll| ≤ 15°, the frame is rotated about the eye midpoint inside the same
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
- `dpi=None` (C5) → the pipeline dpi (e.g. a 400-dpi preset) clamped to 300–600, else 300; pipeline dpi is set to it.
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

## 6. Background & segmentation (`app/imaging/segment.py`)
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

---

## 7. UI (static/, no build step)

**Look — editorial ("Broadsheet")**: a newspaper front page for photos. Nameplate `heyp` with a dateline ("Files stay on
this machine — nothing is uploaded", today's date), a numbered section rail whose numbers are the `1–8` shortcuts, the
picture as "Fig. 1" with a caption, a fact box, printer's crop marks on the crop overlay, a red square marking edited
tools and edit chips. Fonts are self-hosted (SIL OFL, ~208 KB in `static/fonts/`): Fraunces (display), Libre Franklin
(UI), IBM Plex Mono (figures). Colour tokens on `:root` (newsprint `--paper #f4efe4`, `--ink #141210`, editorial red
`--accent #b4141b`, ochre `--warn`, green `--success`) with a dark set via `prefers-color-scheme` + a manual toggle
(persisted in localStorage, try/catch). Errors get a wash + bar + message so they never look like focus. Contrast ≥ 4.5:1
for text. Works from 360 px wide (on phones the picture stays in view while the panel scrolls under it).

**Layout**
```
┌ heyp (nameplate) ── Local only · Add photos · ? · theme ┐
│ dateline                                               │
│ Rail (01–08) │  Canvas (Fig. 1)          │ Panel        │
│  Convert     │  drop zone when empty     │ tool options │
│  Resize      │  hold to compare (band    │  (scrolls)   │
│  Crop        │  under the picture)       │ ──────────── │
│  Passport    │  status · chips · warnings│ Export       │
│  Compress    │  contact strip (48 px)    │ (pinned)     │
│  Edit / Background / Info                │              │
└──────────────┴───────────────────────────┴──────────────┘
```
- **Intake**: drag & drop, file picker, paste (Ctrl/Cmd+V). Files are pre-checked against `/api/health` `limits`
  (size per file, files per request) and uploaded in chunks with progress; thumbnails come from
  `/api/files/{id}/preview?max=240` (browsers can't show HEIC natively).
- **Status line**: `4032×3024 → 1200×900 · 214 KB · JPEG · q 72` (`q` from `encode`) + warnings.
- **Edits model**: global settings; ops are built in fixed order `rotate/flip → crop → background → passport → resize →
  adjust → sheet`. Crop is stored **per file** (fractions) and travels in `ops_by_id`, so every export is one
  `/api/process` request (single file, ZIP, or one combined PDF). Active edits appear as removable chips.
  Passport active ⇒ crop/resize are disabled (it defines the frame).
- **Live preview**: debounced 250 ms `POST /api/preview`; stale responses discarded (sequence + AbortController); busy
  state is the canvas `aria-busy`. A 503 (server busy) is retried after its `Retry-After` (previews, the canvas image
  and thumbnails); 404 removes the expired file.
- **Removing** a photo (×, Del/Backspace) waits 8 s with an Undo toast before the server copy is deleted; leaving the
  page with edits asks first.
- **Tools**
  - *Convert*: big format grid (JPG PNG WebP AVIF HEIC TIFF PDF GIF BMP Original) bound to the Export format; lossless
    (WebP/AVIF), "Combine into one PDF", Advanced (colour, fill, progressive/chroma, file name). No quality or metadata
    controls (those live once, in Export).
  - *Resize*: modes (Dimensions · Percent · Long edge · Print size · DPI only); W/H with a per-photo aspect lock; units
    px/in/cm/mm + DPI; fill/fit/pad/stretch when unlocked; presets (4K, Full HD, HD, Instagram 1080², 1080×1350, story,
    X/Twitter, LinkedIn banner, YouTube thumb, A4 @300, 4×6 in @300); "Don't enlarge" (for Pad: "Photo is not enlarged;
    the canvas is still padded to the exact size"). "DPI only" sets `output.dpi` without resampling.
  - *Crop*: draggable/resizable box snapped to whole pixels, rule-of-thirds grid, keyboard nudging (arrows, Shift = 10 px,
    Alt resizes); ratio chips Free · Original · 1:1 · 4:5 · 3:2 · 16:9 · 9:16 · 35:45 · 2:3 (any a:b swaps on rotate);
    **Auto trim**, **Smart**, **Face**; Reset; Apply to all.
  - *Passport*: searchable country/preset select ("Pick a match" when the search doesn't contain the current one),
    background swatches (white, off-white, light blue, light grey, custom), "Replace background" (off by default and
    explained for presets with `background_edit_allowed=false`; disabled without segmentation), "Level head" (default
    on), "Show guides" (default on: head band, eye band, centre line, measured crown/eye/chin lines from `passport`),
    a measurement readout (head %, eyes %, tilt vs. the preset bands), fine-tune sliders (zoom, up/down, left/right),
    output Single photo | Print sheet (paper, copies ≤ 200, cut lines). Shows preset notes + official spec link.
    **Context view** (Single photo with `passport.source`, not comparing): the frame stays fixed at ~62 % of the stage
    with the whole pre-passport photo around it, dimmed by a veil; dragging (mouse/touch, anywhere on the stage) moves
    the photo under the frame with the finished preview faded out, wheel/pinch/keys resize the head live, and each new
    preview re-lays the photo out from `source` where the user left it. Otherwise the plain view is shown.
  - *Compress*: target max KB (+ optional min KB) with quick chips (20 · 50 · 100 · 200 · 500 KB · 1 MB), format
    (JPG/WebP/AVIF), achieved size, quality used and scale.
  - *Edit*: rotate left/right, flip H/V, straighten (−45…45°), brightness/contrast/saturation/sharpness, grayscale, auto-contrast.
  - *Background*: Remove (transparent → export switches to PNG and back) or Replace with colour.
  - *Info*: dimensions, megapixels, format, size, DPI & print size, colour profile, camera block, GPS chip
    ("Location data present — removed on export" / kept), full EXIF table (collapsible).
- **Export (pinned at the foot of the panel)**: format, quality (shown when a lossy encoder applies), "Keep metadata
  (camera, date)" and "Keep location (GPS)" (both off by default), Download (single → file; many → ZIP; PDF combine).
  Batch warnings from `X-Heyp-Warnings` are summarised in one toast. Exports over `max_files` are refused up front.
- **Keyboard**: `1–8` tools, hold `Space` = original, `R` / `Shift R` rotate, `Ctrl/Cmd+S` download, `Ctrl/Cmd+O` add,
  `[`/`]` previous/next photo, `Del` remove (with Undo), `Esc` revert crop, `?` shortcuts dialog.
- **Accessibility**: real buttons/labels, visible focus, `aria-live` status, `aria-busy` canvas, reduced motion respected,
  no rail transitions.

---

## 8. Docker
- `Dockerfile` multi-stage: `models` (fetch ONNX, size + sha256 pinned; optional `HEYP_MODELS_MIRROR` build arg) →
  `base` (base image pinned by digest; `pip install --require-hashes --no-deps -r requirements.lock && pip check`; numpy
  tests removed; the build loads both models so a broken one fails it) → `dev` (+pytest/httpx from
  `requirements-dev.lock`, `HEYP_REQUIRE_MODELS=1`; source bind-mounted) → `runtime` (non-root uid 10001,
  `MALLOC_ARENA_MAX=2` so freed job memory returns to the OS, `uvicorn
  app.main:app --host 0.0.0.0 --port 8080`, HEALTHCHECK on `/api/health`).
- `.dockerignore` is an allowlist (`app/`, `static/` minus `static/tests/`, the fetch script, the locks) that also
  drops dotfiles, backups and bytecode.
- `docker-compose.yml`: service `heyp`, `ports: "${HEYP_BIND:-127.0.0.1}:${HEYP_PORT:-8080}:8080"` (loopback by default;
  LAN exposure is an explicit opt-in), `mem_limit: ${HEYP_MEM_LIMIT:-4g}`, `pids_limit: 512`, `read_only: true`,
  `tmpfs: /tmp:size=${HEYP_TMP_SIZE:-1g}`, `security_opt: no-new-privileges`, `restart: unless-stopped`, env passthrough
  (`HEYP_MAX_PIXELS` 50000000, `HEYP_MAX_JOBS` 2, `HEYP_CACHE_MB`, `HEYP_ALLOW_CROSS_ORIGIN`, `HEYP_DOCS`, …).
  Memory budget (measured): ~0.45 GB process + models, ≤ ~0.9 GB caches, the tmpfs contents, and per job ~0.9 GB for a
  12 MP background removal up to ~2.25 GB at 50 MP; 4g fits two concurrent 50 MP removals (peak 3.6 GB).
- `scripts/lock.sh` regenerates the locks (and `--base` prints the current base digest); `scripts/smoke.sh` builds/runs
  the runtime image hardened like compose and checks health, limits, HEIC → JPEG, resize, 413/403/404 guards, face,
  background, passport + sheet and delete.
- Tests: `docker run --rm -v "$PWD":/app -w /app heyp:dev pytest -q`; UI unit tests: `node --test static/tests/*.test.mjs`.

## 9. Testing strategy
- **Unit** (per module, synthetic images generated in fixtures; the only binary fixture is `tests/fixtures/astronaut.png`):
  HEIC round-trip incl. EXIF orientation 6 → displayed size swapped exactly once; P3 ICC and HEIC/AVIF nclx → sRGB;
  alpha flatten; every output format decodes back; DPI written (JPEG/PNG/TIFF); every resize mode's exact output
  size (incl. `upscale=false` for pad); crop fraction/px clamping; trim on bordered image; target-size encoder meets
  `max_kb` for 20/50/200 KB and the large-image proxy search stays within its encode cap; preset table sanity (ratios
  in (0,1), min ≤ max, px = mm·dpi/25.4 within 1 px, C3 flags match the notes); sheet copy counts for 2×2 in on 4×6
  (= 6), 35×45 mm on 4×6 (= 8) and on A4 (= 32); passport framing against the fixture's measured ground truth (head,
  chin, crown, roll, top/bottom/side crops, large canvas), the geometry function with fake faces, graceful no-face
  fallback; caches bounded by bytes.
- **API**: upload (multi, bad file → errors list, oversize → 413), preview (`encode`, `passport`), process
  single/zip/pdf incl. `ops_by_id` and batch warnings, 404 after delete, id validation, 422 on bad ops, body caps,
  work budget, busy 503 + `Retry-After`, disk-full 507, cross-origin 403, docs gating, dotfiles.
- **Delivery**: compose defaults and memory budget, `.dockerignore` allowlist, digest/hash pins, model pins; with
  `HEYP_REQUIRE_MODELS=1` a missing model fails the suite instead of skipping.
- **UI**: `node --test static/tests/*.test.mjs` (op builders, `ops_by_id` grouping, limits, warnings summary, retries).
- **E2E (integration)**: `scripts/smoke.sh` on the runtime image, then `curl` a real HEIC through convert/resize/
  compress/passport/sheet/background and the guards against the compose stack; UI smoke in a browser.

## 10. Build plan (how this repo gets built)
1. Scaffold + dependency proof on linux/arm64 (done: HEIC/AVIF/YuNet/U²-Net all load).
2. Parallel implementation against this contract: imaging core · face/segment/passport (+ researched presets) · API/store/pipeline · UI.
3. Integration: runtime image, full test suite, E2E with real HEIC, browser smoke test.
4. Multi-lens review (correctness, security, passport accuracy, UX, delivery) with adversarial verification → v1.1 fix
   round against CONTRACT-v1.1 → re-test (done).

## 11. Roadmap (post-v1.1)
Watermark/text overlay · collage & contact sheets · RAW (DNG/CR2/NEF via rawpy) · AI upscaling (Real-ESRGAN ONNX) ·
document scan mode (perspective deskew) · signature cleanup (for exam forms) · PWA/offline install · CLI wrapper
(`docker run heyp convert …`) · optional basic-auth for LAN exposure · HDR (PQ/HLG) tone mapping · an int8 U²-Net
option once a portrait corpus confirms edge quality · hard per-job time limits ·
more presets: US green card, New Zealand passport, UAE, Saudi Arabia, Pakistan, Bangladesh, Nepal, Brazil, Mexico,
Russia, Philippines, Nigeria.
