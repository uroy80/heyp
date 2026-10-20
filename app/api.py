"""The image API under ``/api`` (PLAN.md §3): uploads, previews, exports, presets and file access.

Endpoints are plain ``def`` so Starlette runs them in its threadpool; CPU-heavy sections are
additionally admitted by ``app.state.jobs`` (:class:`JobSlots`: HEYP_MAX_JOBS at once, their planned
memory within a budget, and a bounded wait queue so queued requests can't starve the threadpool).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
import json
import logging
import re
import shutil
import tempfile
import threading
import time
import unicodedata
import zipfile
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Annotated, Any, BinaryIO
from urllib.parse import quote

import anyio.from_thread
from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from PIL import Image, UnidentifiedImageError

from app.config import VERSION, Settings
from app.core import files as kinds
from app.core.errors import RateLimited
from app.core.limits import client_info
from app.imaging import info as imginfo, io, pipeline, presets
from app.schemas import INPUT_FORMATS, OUTPUT_FORMATS, ProcessRequest
from app.store import FileStore, StoredFile, StoreError, clean_name

router = APIRouter(prefix="/api")
log = logging.getLogger("heyp")

JOB_WAIT_SECONDS = 300
HEAVY_THREADS = 24                 # heavy requests (running + waiting) that may hold a threadpool thread
MIN_WAITING = 8                    # anyio's pool has 40; the rest stays free for static files and light calls
BUSY_DETAIL = "Server busy, try again shortly"
RETRY_AFTER_SECONDS = 5
REQUEST_WORK_BUDGET = 3600         # estimated CPU seconds (pipeline.plan) one request may ask for
DECODE_BYTES_PER_PIXEL = 8         # decoding briefly holds two 4 B/px copies
ZIP_SPOOL_BYTES = 32 * 1024 * 1024
ZIP_MAX_BYTES = 2 * 1024 ** 3      # ZIPs are also capped at half the free space of the upload volume
ZIP_MIN_BYTES = 64 * 1024 * 1024
STREAM_CHUNK = 1024 * 1024
PREVIEW_BUCKETS = (240, 480, 960, 1600)  # thumbnail sides cached on disk (the UI asks for 240 and 1600)
DISPLAY_DECODE_MAX_PIXELS = 12_000_000   # larger outputs are re-decoded at reduced size for /preview
# Header probes only use the decoders io.load accepts (no exotic parsers on untrusted uploads).
PROBE_FORMATS = ("JPEG", "MPO", "PNG", "WEBP", "AVIF", "HEIF", "TIFF", "BMP", "DIB", "GIF", "ICO", "JPEG2000")
_UPLOAD_STATUS_PRIORITY = (507, 503, 429, 413, 415, 422, 400)
# Inputs beyond the encoder list (the extension is only a hint, bytes decide): layered, vector and camera RAW
# images, and HTML pages for the HTML → image tool.
IMAGE_INPUTS_EXTRA = ["psd", "psb", "svg", "svgz", *sorted(kinds.RAW_EXTS)]
HTML_INPUTS = ["html", "htm"]
_NO_SPACE = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}

PreviewMax = Annotated[int | None, Query(alias="max", ge=16, le=16384, description="max side in px")]


# ------------------------------------------------------------------ job admission
class Busy(Exception):
    """The wait queue is full."""


class JobSlots:
    """First-come-first-served admission for heavy work.

    At most ``max_jobs`` run at once and their planned bytes stay within ``memory_bytes`` (a job
    larger than the whole budget runs alone). Requests not yet admitted are refused with
    :class:`Busy` once ``max_waiting`` are queued, so waiting never ties up the whole threadpool.
    """

    def __init__(self, max_jobs: int, memory_bytes: int, max_waiting: int) -> None:
        self.max_jobs = max(1, int(max_jobs))
        self.memory_bytes = max(1, int(memory_bytes))
        self.max_waiting = max(0, int(max_waiting))
        self._cond = threading.Condition()
        self._queue: deque[object] = deque()
        self._running = 0
        self._used = 0

    def acquire(self, weight: int = 0, *, timeout: float, bounded: bool = True) -> int | None:
        """Wait for a slot; returns the weight to hand to :meth:`release`, or None on timeout."""
        weight = min(max(0, int(weight)), self.memory_bytes)
        deadline = time.monotonic() + timeout
        ticket = object()
        with self._cond:
            if bounded and self._queue and len(self._queue) >= self.max_waiting:
                raise Busy()
            self._queue.append(ticket)
            try:
                while not (self._queue[0] is ticket and self._fits(weight)):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._cond.wait(remaining)
                self._running += 1
                self._used += weight
                return weight
            finally:
                self._queue.remove(ticket)
                self._cond.notify_all()

    def release(self, weight: int) -> None:
        with self._cond:
            self._running -= 1
            self._used -= weight
            self._cond.notify_all()

    def stats(self) -> dict[str, int]:
        with self._cond:
            return {"running": self._running, "waiting": len(self._queue), "planned_bytes": self._used}

    def _fits(self, weight: int) -> bool:
        return self._running < self.max_jobs and (self._running == 0 or self._used + weight <= self.memory_bytes)


def make_job_slots(settings: Settings) -> JobSlots:
    return JobSlots(settings.max_jobs, settings.job_memory_bytes, max(MIN_WAITING, HEAVY_THREADS - settings.max_jobs))


def _busy() -> HTTPException:
    return HTTPException(503, BUSY_DETAIL, headers={"Retry-After": str(RETRY_AFTER_SECONDS)})


@contextmanager
def _job(request: Request, weight: int = 0) -> Iterator[None]:
    """Hold one job slot planned at ``weight`` bytes. Only a request's first slot can be refused as busy,
    so a batch already under way is never cut off between its items."""
    slots: JobSlots = request.app.state.jobs
    admitted = getattr(request.state, "heyp_admitted", False)
    try:
        held = slots.acquire(weight, timeout=JOB_WAIT_SECONDS, bounded=not admitted)
    except Busy:
        raise _busy() from None
    if held is None:
        raise _busy()
    request.state.heyp_admitted = True
    try:
        yield
    finally:
        slots.release(held)


# ------------------------------------------------------------------ plumbing
def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _store(request: Request) -> FileStore:
    return request.app.state.store


def error_status(exc: BaseException, *, decoding: bool = False) -> tuple[int, str] | None:
    """Map a domain exception to ``(status, detail)``; None for unexpected errors (→ 500)."""
    if isinstance(exc, HTTPException):
        return exc.status_code, str(exc.detail)
    if isinstance(exc, StoreError | pipeline.PipelineError):
        return exc.status, exc.detail
    if isinstance(exc, io.ImageError):
        return int(getattr(exc, "status", 415)), str(exc) or "Unsupported or corrupt image"
    if isinstance(exc, Image.DecompressionBombError):
        return 413, "Image has too many pixels"
    if isinstance(exc, MemoryError):
        return 413, "Image too large to process"
    if isinstance(exc, UnidentifiedImageError):
        return 415, "Unsupported or corrupt image"
    if isinstance(exc, OSError) and exc.errno in _NO_SPACE:
        return 507, "Server storage full"
    if decoding and isinstance(exc, (OSError, ValueError, SyntaxError, EOFError)):
        return 415, "Unsupported or corrupt image"
    if isinstance(exc, ValueError):
        return 422, str(exc) or "Invalid request"
    if isinstance(exc, OSError) and exc.errno is None:  # codec/library failure while encoding
        return 422, f"Could not encode image: {exc}"
    return None


@contextmanager
def _errors(prefix: str = "") -> Iterator[None]:
    """Translate domain exceptions into HTTPException; ``prefix`` names the file in batch jobs."""
    try:
        yield
    except HTTPException as exc:
        if prefix:
            raise HTTPException(exc.status_code, prefix + str(exc.detail), headers=exc.headers) from None
        raise
    except Exception as exc:
        mapped = error_status(exc)
        if mapped is None:
            raise
        raise HTTPException(mapped[0], prefix + mapped[1]) from None


def _feature(module: str) -> bool:
    try:
        if module == "face":
            from app.imaging import face as mod
        else:
            from app.imaging import segment as mod
        return bool(mod.available())
    except Exception:  # ImportError or a broken model → just report unavailable
        return False


def _probe_size(path: Path) -> tuple[int, int] | None:
    """Pixel size from the file header without decoding (None if the header can't be read)."""
    try:
        with Image.open(path, formats=[f for f in PROBE_FORMATS if f in Image.OPEN]) as im:
            return im.size
    except Exception:  # unreadable here → the real decoder reports the problem
        return None


def _pixels(stored: StoredFile, settings: Settings) -> int:
    """Pixel count of an upload: recorded at upload, else from its header, else the limit."""
    size = (stored.width, stored.height) if stored.width and stored.height else _probe_size(stored.path)
    return size[0] * size[1] if size else settings.max_pixels


def _decode_weight(stored: StoredFile, settings: Settings) -> int:
    return DECODE_BYTES_PER_PIXEL * _pixels(stored, settings)


def _client_gone(request: Request) -> bool:
    """True once the client has disconnected (checked between the items of a batch)."""
    try:
        return bool(anyio.from_thread.run(request.is_disconnected))
    except Exception:  # not running in a worker thread (e.g. called directly): assume still there
        return False


# ------------------------------------------------------------------ naming
def _stem(name: str) -> str:
    stem, dot, _ext = name.rpartition(".")
    return stem if dot and stem else name


def output_name(original: str, fmt: str, meta: dict[str, Any], override: str | None = None) -> str:
    """``<stem>.<ext>`` (+ ``_passport-<preset>`` / ``_sheet-<paper>``), sanitised."""
    ext = io.ext_for(fmt)
    if override:
        stem = override
    else:
        stem = _stem(clean_name(original))
        if meta.get("passport_preset"):
            stem += f"_passport-{meta['passport_preset']}"
        if meta.get("sheet_paper"):
            stem += f"_sheet-{meta['sheet_paper']}"
    return clean_name(f"{stem}.{ext}", default=f"image.{ext}")


def dedupe_name(name: str, used: set[str]) -> str:
    """``x.jpg``, ``x (2).jpg``, … (case-insensitive, records the result in ``used``)."""
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    candidate, n = name, 1
    while candidate.lower() in used:
        n += 1
        candidate = f"{stem} ({n}).{ext}" if dot else f"{stem} ({n})"
    used.add(candidate.lower())
    return candidate


def _ascii(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Za-z0-9._()\- ]", "_", text).strip(" .")


def content_disposition(filename: str) -> str:
    """``attachment`` with an ASCII fallback and an RFC 5987 ``filename*``."""
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        stem, ext = filename, ""
    ascii_stem, ascii_ext = _ascii(stem), _ascii(ext)
    if not ascii_stem.strip("_ ."):
        ascii_stem = "heyp"
    fallback = f"{ascii_stem}.{ascii_ext}" if ascii_ext else ascii_stem
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


# ------------------------------------------------------------------ static info
# Cheap endpoints are async so they never queue for a threadpool slot behind heavy jobs.
def model_features() -> dict[str, bool]:
    """Optional image models without loading any of them; all False when unavailable."""
    try:
        from app.tools import img_ai

        found = img_ai.features()
    except Exception:  # module missing or broken → nothing optional is on
        found = {}
    return {key: bool(found.get(key, False)) for key in ("upscale", "matting", "plates")}


def engine_features() -> dict[str, bool]:
    """Renderer (HTML → image) availability as the tool registry sees it (a cached health check)."""
    from app.core import registry

    return {"renderer": registry.engine_status("renderer")[0]}


@router.get("/health")
async def health(request: Request) -> dict:
    """Liveness plus what this server can do. Async (never queues behind jobs); the renderer health
    check, which touches its socket, runs in a worker thread outside the request threadpool."""
    from app.routers.config import upscale_max_input_pixels

    settings = _settings(request)
    engines = await asyncio.to_thread(engine_features)
    return {
        "status": "ok",
        "version": VERSION,
        "revision": settings.revision or None,
        "features": {
            "heic": bool(io.FEATURES.get("heic", False)),
            "avif": bool(io.FEATURES.get("avif", False)),
            "face": _feature("face"),
            "segmentation": _feature("segment"),
            **model_features(),
            **engines,
        },
        "limits": {
            "max_upload_mb": settings.max_upload_mb,
            "max_files": settings.max_files,
            "max_pixels": min(settings.max_pixels, io.max_pixels()),
            "upscale_max_input_pixels": upscale_max_input_pixels(),
            **({"max_request_mb": settings.max_request_mb} if settings.max_request_mb else {}),
        },
    }


@router.get("/formats")
async def formats() -> dict:
    """``input``/``output``: the decoder and encoder lists; ``images`` adds layered, vector and RAW inputs and
    ``html`` the pages the HTML → image tool captures."""
    return {"input": list(INPUT_FORMATS), "output": list(OUTPUT_FORMATS), "images": IMAGE_INPUTS_EXTRA,
            "html": HTML_INPUTS}


@router.get("/presets/passport")
async def passport_presets() -> dict:
    return {"presets": presets.list_presets(), "papers": presets.list_papers()}


# ------------------------------------------------------------------ files
def kind_fields(stored: StoredFile) -> dict[str, Any]:
    """The FileInfo fields every file carries (kind, origin, deletion deadline), images included."""
    info = stored.info()
    return {key: info[key] for key in ("kind", "source", "parent_ids", "tool", "expires_at")}


def image_file_info(stored: StoredFile, described: dict[str, Any]) -> dict[str, Any]:
    """FileInfo of an image: its description (format, size, EXIF …) plus the kind fields."""
    return {"id": stored.id, **described, **kind_fields(stored)}


def _image_size(path: Path, name: str | None) -> tuple[int, int] | None:
    """Pixel size from the header (SVG: the size it is rasterised at; RAW: unknown → None)."""
    try:
        size = io.probe_size(str(path))
    except Exception:
        size = None
    return size or _probe_size(path)


def _accept_param(raw: str | None) -> frozenset[str] | None:
    try:
        return kinds.parse_accept(raw)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


AcceptQuery = Annotated[str | None, Query(max_length=64, description="comma list of kinds this page takes, "
                                                                     "e.g. image or pdf,office")]


@router.post("/files")
def upload_files(request: Request, files: Annotated[list[UploadFile], File(description="files")],
                 accept: AcceptQuery = None) -> Any:
    """Store each upload, decide its kind from its bytes and describe it; failures are per file."""
    settings, store = _settings(request), _store(request)
    accepted = _accept_param(accept)
    if len(files) > settings.max_files:
        raise HTTPException(413, f"Too many files: {len(files)} (max {settings.max_files} per upload)")
    limiter = getattr(request.app.state, "limiter", None)
    client = client_info(request, settings) if limiter is not None and limiter.enabled else None
    ok: list[dict] = []
    errors: list[dict] = []
    statuses: list[int] = []
    retry_after = 0

    def fail(name: str, status: int, detail: str) -> None:
        errors.append({"name": name, "detail": detail})
        statuses.append(status)

    for upload in files:
        name = clean_name(upload.filename)
        try:
            stored = store.save(upload.filename, upload.file, max_bytes=settings.max_upload_bytes)
        except StoreError as exc:
            fail(name, exc.status, exc.detail)
            continue
        if limiter is not None and client is not None:
            try:  # hosted mode: bytes per client per hour (counted even when the file is refused below)
                limiter.check("upload_bytes", client, stored.size_bytes)
            except RateLimited as exc:
                store.delete(stored.id)
                retry_after = max(retry_after, exc.retry_after)
                fail(name, 429, exc.detail)
                continue
        try:
            sniffed = kinds.sniff(stored.path, upload.filename)
        except kinds.FileKindError as exc:
            store.delete(stored.id)
            fail(name, exc.status, exc.detail)
            continue
        if sniffed.kind == "other":
            store.delete(stored.id)
            fail(name, 415, kinds.unsupported_detail(upload.filename))
            continue
        if accepted is not None and sniffed.kind not in accepted:
            store.delete(stored.id)
            fail(name, 415, kinds.refused_kind_detail(sniffed, accepted))
            continue
        if sniffed.kind == "html":  # the HTML → image tool's input: stored as it is, never parsed here
            store.set_info(stored.id, kind="html", format=sniffed.format, mime=sniffed.mime)
            ok.append(store.get(stored.id).info())
            continue
        size = _image_size(stored.path, upload.filename)
        if size and size[0] * size[1] > settings.max_pixels:  # refuse before decoding anything
            store.delete(stored.id)
            fail(name, 413, f"Image is {size[0]}×{size[1]} px ({size[0] * size[1] / 1e6:.0f} MP), "
                            f"above the {settings.max_pixels / 1e6:.0f} MP limit")
            continue
        try:
            weight = DECODE_BYTES_PER_PIXEL * (size[0] * size[1] if size else settings.max_pixels)
            with _job(request, weight):
                loaded = io.load(str(stored.path), name=upload.filename)
                described = imginfo.describe(loaded, name=stored.name, size_bytes=stored.size_bytes)
        except Exception as exc:
            store.delete(stored.id)
            mapped = error_status(exc, decoding=True)  # incl. 503 when the job queue is full
            if mapped is None:  # unexpected decoder failure: still the file's fault, but note the type
                log.warning("upload decode failed: %s", type(exc).__name__)
                mapped = (415, "Unsupported or corrupt image")
            fail(name, *mapped)
            continue
        store.set_info(stored.id, kind="image", format=loaded.format, mime=described.get("mime") or sniffed.mime,
                       width=loaded.image.width, height=loaded.image.height, frames=int(loaded.frames or 1))
        store.cache_put(stored.id, loaded)
        ok.append(image_file_info(store.get(stored.id), described))

    body = {"files": ok, "errors": errors}
    if ok or not errors:
        return body
    status = next((s for s in _UPLOAD_STATUS_PRIORITY if s in statuses), statuses[0])
    detail = errors[statuses.index(status)]["detail"]
    headers = None
    if status == 503:
        headers = {"Retry-After": str(RETRY_AFTER_SECONDS)}
    elif status == 429:
        headers = {"Retry-After": str(max(1, retry_after))}
    return JSONResponse({"detail": detail, **body}, status_code=status, headers=headers)


def _require_image(stored: StoredFile) -> None:
    """415 for a non-image file (an HTML page, a PDF or ZIP result) where an image is needed."""
    if stored.kind != "image":
        what = kinds.KIND_NAMES.get(stored.kind, "not an image")
        raise HTTPException(415, f"{stored.name} is {what}, not an image")


@router.get("/files/{file_id}")
def file_info(file_id: str, request: Request) -> dict:
    """FileInfo; images are described in full (EXIF included), other files from their stored metadata."""
    settings, store = _settings(request), _store(request)
    with _errors():
        stored = store.get(file_id)
        if stored.kind != "image":
            return stored.info()
        with _job(request, _decode_weight(stored, settings)):
            loaded = store.load(file_id)
            described = imginfo.describe(loaded, name=stored.name, size_bytes=stored.size_bytes, full_exif=True)
    return image_file_info(stored, described)


@router.get("/files/{file_id}/download")
def file_download(file_id: str, request: Request) -> Response:
    """The stored bytes (an upload, or a job result for "Continue with" / saving), as an attachment."""
    with _errors():
        stored = _store(request).get(file_id)
        handle = open(stored.path, "rb")  # noqa: SIM115 — closed by the streaming iterator
    size = stored.size_bytes
    with contextlib.suppress(OSError):
        size = Path(stored.path).stat().st_size
    headers = {"Content-Disposition": content_disposition(stored.name), "Content-Length": str(size)}
    return StreamingResponse(_iter_file(handle), media_type=stored.mime or "application/octet-stream",
                             headers=headers)


def _preview_bucket(side: int, settings: Settings) -> bool:
    """Only a few thumbnail sides are cached on disk, so arbitrary ?max values can't fill the volume."""
    return side == settings.preview_max or side in PREVIEW_BUCKETS


@router.get("/files/{file_id}/preview")
def file_preview(file_id: str, request: Request, max_side: PreviewMax = None) -> Response:
    settings, store = _settings(request), _store(request)
    side = min(max_side or settings.preview_max, settings.preview_max)
    cacheable = _preview_bucket(side, settings)
    with _errors():
        stored = store.get(file_id)
        _require_image(stored)
        for ext, mime in (("jpg", "image/jpeg"), ("png", "image/png")) if cacheable else ():
            cached = store.read_aux(file_id, f"preview-{side}.{ext}")
            if cached is not None:
                data = cached
                break
        else:
            with _job(request, _decode_weight(stored, settings)):
                loaded = store.load(file_id)
                data, mime = io.preview(loaded.image, loaded.icc_profile, max_side=side)
            if cacheable:
                store.write_aux(file_id, f"preview-{side}.{'png' if mime == 'image/png' else 'jpg'}", data)
    return Response(content=data, media_type=mime, headers={"Cache-Control": "private, max-age=3600"})


@router.get("/files/{file_id}/faces")
def file_faces(file_id: str, request: Request) -> dict:
    settings, store = _settings(request), _store(request)
    with _errors():
        stored = store.get(file_id)
        _require_image(stored)
        try:
            from app.imaging import face
        except ImportError:
            face = None
        if face is None or not face.available():  # "no detector" must not look like "no faces"
            raise HTTPException(503, "Face detection is unavailable")
        with _job(request, _decode_weight(stored, settings)):
            loaded = store.load(file_id)
            faces = face.detect(loaded.image)
    width, height = loaded.image.size
    return {"width": width, "height": height,
            "faces": [f.to_dict() if hasattr(f, "to_dict") else f for f in faces]}


@router.delete("/files/{file_id}", status_code=204)
async def delete_file(file_id: str, request: Request) -> Response:
    """Async on purpose: removing a directory is quick and must not queue behind heavy jobs."""
    with _errors():
        if not _store(request).delete(file_id):
            raise HTTPException(404, "File not found or expired")
    return Response(status_code=204)


# ------------------------------------------------------------------ processing
def _check_request(request: Request, req: ProcessRequest) -> list[StoredFile]:
    """Id count (this app's HEYP_MAX_FILES), existence, and the total work budget."""
    settings, store = _settings(request), _store(request)
    if len(req.ids) > settings.max_files:
        raise HTTPException(422, f"too many ids: {len(req.ids)} (max {settings.max_files})")
    with _errors():
        stored = [store.get(file_id) for file_id in req.ids]
        for item in stored:
            _require_image(item)
    return stored


def _frames(item: StoredFile, req: ProcessRequest) -> int:
    """Frames the pipeline will process for ``item`` (1 unless an animation is written)."""
    frames = int((item.meta or {}).get("frames") or 1)
    return frames if pipeline.animated_output(item.format, frames, req.output) else 1


def _plans(request: Request, stored: list[StoredFile], req: ProcessRequest) -> list[pipeline.Plan]:
    """Estimate every item before any work starts; 413 when the whole request is too heavy."""
    settings = _settings(request)
    plans = []
    for item in stored:
        size = (item.width, item.height) if item.width and item.height else _probe_size(item.path)
        if size is None:
            side = int(settings.max_pixels ** 0.5)
            size = (side, side)
        with _errors(f"{item.name}: " if len(stored) > 1 else ""):
            frames = _frames(item, req)
            if frames > 1:  # frame-bomb guard before anything is decoded (413)
                pipeline.check_animation_size(frames, int(size[0]), int(size[1]), max_frames=settings.max_anim_frames,
                                              max_pixels=settings.max_anim_pixels)
            plans.append(pipeline.plan(size, req.ops_for(item.id), req.output, source_format=item.format or "jpeg",
                                       frames=frames))
    work = sum(p.work for p in plans)
    if work > REQUEST_WORK_BUDGET:
        raise HTTPException(413, f"This request is too much work for one go (about {work / 60:.0f} min of "
                                 f"processing; the limit is {REQUEST_WORK_BUDGET / 60:.0f} min). Process fewer "
                                 "files at once, or use smaller sizes or a faster format.")
    return plans


def _run(request: Request, stored: StoredFile, req: ProcessRequest) -> tuple[bytes, str, dict, pipeline.Ctx]:
    """Load, run and encode one file (the caller holds a job slot)."""
    store = _store(request)
    loaded = store.load(stored.id)
    settings = _settings(request)
    animation = None
    if pipeline.wants_animation(loaded, req.output):  # every frame (GIF/WebP/APNG → animation)
        animation = pipeline.load_animation(str(stored.path), max_frames=settings.max_anim_frames,
                                            max_pixels=settings.max_anim_pixels)
    ctx = pipeline.run(loaded, req.ops_for(stored.id), cache_key=stored.id,
                       max_pixels=settings.max_pixels, alive=lambda: store.exists(stored.id),
                       animation=animation, resolve_file=_resolver(request),
                       max_anim_pixels=settings.max_anim_pixels)
    data, fmt, info = pipeline.render(ctx, req.output)
    frames = int((stored.meta or {}).get("frames") or 1)
    if animation is None and frames > 1 and not pipeline.animated_output(stored.format, frames, req.output):
        still = pipeline.still_warning(fmt)
        if still not in info["warnings"]:
            info["warnings"].append(still)
    return data, fmt, info, ctx


def _resolver(request: Request) -> Any:
    """Ops may reference another upload (e.g. a watermark image): an image that exists → its Loaded."""
    store = _store(request)

    def resolve(file_id: str) -> Any:
        try:
            stored = store.get(file_id)
        except StoreError:
            raise HTTPException(422, "A file used by this edit (e.g. a watermark image) was not found or has "
                                     "expired; upload it again") from None
        _require_image(stored)
        return store.load(file_id)

    return resolve


def _display_image(data: bytes, fmt: str, ctx: pipeline.Ctx, side: int) -> tuple[bytes, str]:
    """Downscaled view of what was actually encoded, so compression artefacts are visible.

    Outputs above DISPLAY_DECODE_MAX_PIXELS are not decoded at full size again: JPEG is decoded
    DCT-scaled (draft), other formats are shown from the pre-encode image.
    """
    if fmt != "pdf":
        try:
            with Image.open(BytesIO(data), formats=[f for f in PROBE_FORMATS if f in Image.OPEN]) as im:
                width, height = im.size
                if width * height > DISPLAY_DECODE_MAX_PIXELS and im.format == "JPEG":
                    im.draft("L" if im.mode == "L" else "RGB", (side, side))
                    im.load()
                    return io.preview(im, im.info.get("icc_profile"), max_side=side)
            if width * height <= DISPLAY_DECODE_MAX_PIXELS:
                decoded = io.load(data)
                return io.preview(decoded.image, decoded.icc_profile, max_side=side)
        except Exception:  # undecodable by our own reader → fall back to the pre-encode image
            pass
    return io.preview(ctx.image, ctx.icc_profile, max_side=side)


@router.post("/preview")
def preview(req: ProcessRequest, request: Request, max_side: PreviewMax = None) -> dict:
    """Full-resolution pipeline + encode (exact size), plus a small display image, in one job slot."""
    settings = _settings(request)
    if len(req.ids) != 1:
        raise HTTPException(422, "preview takes exactly one id")
    side = min(max_side or settings.preview_max, settings.preview_max)
    (stored,) = _check_request(request, req)
    (plan,) = _plans(request, [stored], req)
    with _errors(), _job(request, plan.peak_bytes):
        data, fmt, info, ctx = _run(request, stored, req)
        shown, mime = _display_image(data, fmt, ctx, side)
    return {
        "width": info["width"],
        "height": info["height"],
        "format": fmt,
        "size_bytes": len(data),
        "dpi": info["dpi"],
        "warnings": info["warnings"],
        "image": f"data:{mime};base64,{base64.b64encode(shown).decode('ascii')}",
        "encode": info.get("encode"),
        "passport": pipeline.passport_guides(ctx),
    }


HOSTED_JOB_OPS = frozenset({"upscale", "background"})  # hosted mode: only as a job (img.process)
HOSTED_SYNC_WORK = 60.0  # hosted mode: estimated CPU seconds one synchronous export may take (proxies cut at 60–100 s)
USE_JOB_HEADER = "X-Heyp-Use-Job"


def _hosted_sync_guard(request: Request, req: ProcessRequest, plans: list[pipeline.Plan]) -> None:
    """Hosted mode: refuse exports that could outlive the reverse proxy's timeout (Cloudflare 100 s, Apache 60 s)
    — they run as the ``img.process`` job instead — and count each export against the client's job bucket."""
    settings = _settings(request)
    if not settings.hosted:
        return
    lists = [req.ops, *(req.ops_by_id or {}).values()]
    heavy = sorted({op.op for ops in lists for op in ops if op.op in HOSTED_JOB_OPS})
    work = sum(p.work for p in plans)
    if heavy or work > HOSTED_SYNC_WORK:
        why = f"{' and '.join(heavy)} runs" if heavy else "this export takes"
        raise HTTPException(422, f"On this server {why} as a background job: POST /api/jobs with tool "
                                 "img.process (the same ops and output) to get progress and the result",
                            headers={USE_JOB_HEADER: "img.process"})
    limiter = getattr(request.app.state, "limiter", None)
    if limiter is not None and limiter.enabled:
        try:
            limiter.check("jobs", client_info(request, settings))
        except RateLimited as exc:
            raise HTTPException(429, exc.detail, headers={"Retry-After": str(exc.retry_after)}) from None


@router.post("/process")
def process(req: ProcessRequest, request: Request) -> Response:
    """One id → the file; several → ZIP (or one PDF with ``format=pdf`` + ``combine_pdf``)."""
    stored = _check_request(request, req)
    with _store(request).pinned(req.ids):  # nothing expires half-way through a long batch
        plans = _plans(request, stored, req)
        _hosted_sync_guard(request, req, plans)
        if len(stored) == 1:
            return _process_single(request, stored[0], req, plans[0])
        if req.output.format == "pdf" and req.output.combine_pdf:
            return _process_combined_pdf(request, stored, req, plans)
        return _process_zip(request, stored, req, plans)


def _process_single(request: Request, stored: StoredFile, req: ProcessRequest, plan: pipeline.Plan) -> Response:
    with _errors(), _job(request, plan.peak_bytes):
        data, fmt, info, ctx = _run(request, stored, req)
    name = output_name(stored.name, fmt, ctx.meta, req.output.filename)
    headers = {
        "Content-Disposition": content_disposition(name),
        "X-Heyp-Width": str(info["width"]),
        "X-Heyp-Height": str(info["height"]),
        "X-Heyp-Warnings": warnings_header(info["warnings"]),
    }
    return Response(content=data, media_type=io.mime_for(fmt), headers=headers)


WARNINGS_HEADER_MAX = 4000  # bytes of URL-encoded JSON; a ZIP of many files keeps the first ones


def warnings_header(warnings: list[str]) -> str:
    """``X-Heyp-Warnings`` value: URL-encoded JSON list, trimmed to whole entries within the cap."""
    kept: list[str] = []
    for message in warnings:
        trial = quote(json.dumps(kept + [message], ensure_ascii=False), safe="")
        if len(trial) > WARNINGS_HEADER_MAX:
            more = len(warnings) - len(kept)
            kept.append(f"…and {more} more warning{'s' if more != 1 else ''}")
            break
        kept.append(message)
    return quote(json.dumps(kept, ensure_ascii=False), safe="")


def _batch_warn(collected: list[str], name: str, warnings: list[str]) -> None:
    for message in warnings:
        line = f"{name}: {message}"
        if line not in collected:
            collected.append(line)


class ClientGone(HTTPException):
    def __init__(self) -> None:
        super().__init__(499, "Client closed the request")


def _process_combined_pdf(request: Request, stored: list[StoredFile], req: ProcessRequest,
                          plans: list[pipeline.Plan]) -> Response:
    settings, store = _settings(request), _store(request)
    images: list[Image.Image] = []
    total = 0
    first_dpi: tuple[float, float] | None = None
    warned: list[str] = []
    for item, plan in zip(stored, plans):
        if images and _client_gone(request):
            raise ClientGone()
        with _errors(f"{item.name}: "), _job(request, plan.peak_bytes):
            ctx = pipeline.run(store.load(item.id), req.ops_for(item.id), cache_key=item.id,
                               max_pixels=settings.max_pixels, alive=lambda i=item.id: store.exists(i),
                               resolve_file=_resolver(request))
            img = ctx.image
            if req.output.colorspace == "srgb":
                img = io.to_srgb(img, ctx.icc_profile)
        _batch_warn(warned, item.name, ctx.warnings)
        total += img.width * img.height
        if total > settings.max_pixels:
            raise HTTPException(413, f"Too many pixels for one PDF (limit {settings.max_pixels / 1e6:.0f} MP); "
                                     "reduce the size or turn off 'Combine into one PDF'")
        images.append(img)
        first_dpi = first_dpi or ctx.dpi
    dpi = req.output.dpi or (round(first_dpi[0]) if first_dpi else pipeline.PDF_DEFAULT_DPI)
    with _errors(), _job(request, 4 * total):
        data = io.save_pdf(images, dpi=dpi, background=req.output.background)
    name = clean_name(f"{req.output.filename or f'heyp-{len(stored)}-files'}.pdf")
    headers = {"Content-Disposition": content_disposition(name)}
    if warned:
        headers["X-Heyp-Warnings"] = warnings_header(warned)
    return Response(content=data, media_type=io.mime_for("pdf"), headers=headers)


def _zip_cap(root: Path) -> int:
    """Largest ZIP spooled at once: ZIP_MAX_BYTES, but never more than half the free space."""
    try:
        free = shutil.disk_usage(root).free
    except OSError:
        return ZIP_MAX_BYTES
    return max(ZIP_MIN_BYTES, min(ZIP_MAX_BYTES, free // 2))


def _process_zip(request: Request, stored: list[StoredFile], req: ProcessRequest,
                 plans: list[pipeline.Plan]) -> StreamingResponse:
    store = _store(request)
    root = store.root if store.root.is_dir() else Path(tempfile.gettempdir())
    cap = _zip_cap(root)
    spool = tempfile.SpooledTemporaryFile(max_size=ZIP_SPOOL_BYTES, dir=str(root))
    archive = zipfile.ZipFile(spool, "w", compression=zipfile.ZIP_STORED, allowZip64=True)
    try:
        used: set[str] = set()
        warned: list[str] = []
        for index, (item, plan) in enumerate(zip(stored, plans)):
            if index and _client_gone(request):
                raise ClientGone()
            with _errors(f"{item.name}: "), _job(request, plan.peak_bytes):
                data, fmt, info, ctx = _run(request, item, req)
            _batch_warn(warned, item.name, info["warnings"])
            if spool.tell() + len(data) > cap:
                raise HTTPException(413, f"The ZIP would be larger than {cap // 2**20} MB; download "
                                         "fewer files at once or choose a smaller format")
            name = dedupe_name(output_name(item.name, fmt, ctx.meta, req.output.filename), used)
            entry = zipfile.ZipInfo(name, date_time=time.localtime()[:6])
            entry.compress_type = zipfile.ZIP_STORED
            with _errors(f"{item.name}: "):
                archive.writestr(entry, data)
            del data, ctx
        with _errors():
            archive.close()  # writes the central directory
        size = spool.tell()
        spool.seek(0)
    except BaseException:
        for handle in (archive, spool):  # both flush on close; on a full disk that raises again
            with contextlib.suppress(OSError, ValueError):
                handle.close()
        raise
    name = clean_name(f"{req.output.filename or f'heyp-{len(stored)}-files'}.zip")
    headers = {"Content-Disposition": content_disposition(name), "Content-Length": str(size)}
    if warned:  # "<file name>: <warning>" per item (single files send theirs unprefixed)
        headers["X-Heyp-Warnings"] = warnings_header(warned)
    return StreamingResponse(_iter_file(spool), media_type="application/zip", headers=headers)


def _iter_file(handle: BinaryIO) -> Iterator[bytes]:
    try:
        while chunk := handle.read(STREAM_CHUNK):
            yield chunk
    finally:
        handle.close()
