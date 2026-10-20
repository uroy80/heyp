"""heyp AI tools that are not plain pipeline ops (PLAN.md §6, §8).

1. ``GET /api/detect/{file_id}?what=faces,plates&level=recommended`` → boxes the Blur tool pre-selects::

    {"file_id", "width", "height", "what": ["faces", "plates"], "level": "recommended",
     "regions": [{"kind": "face"|"plate", "x", "y", "w", "h",      # fractions of the EXIF-oriented image,
                  "shape": "ellipse"|"rect", "score", "selected"}],  # already grown to cover hair/chin/plate rim
     "counts": {"faces": n, "plates": n}, "unavailable": ["plates"], "warnings": [...]}

   ``regions`` can be passed straight to the ``blur_regions`` op (``selected`` / ``kind`` / ``score`` are
   ignored there). ``level``: ``low`` (one fast pass, confident boxes only), ``recommended`` (adds
   1280-px tiles: small faces and distant plates) or ``high`` (lower thresholds; more false positives,
   unselected). Errors: 404 unknown/expired file, 422 not an image or bad query, 413/415 image errors,
   429 rate limit (hosted), 503 when none of the requested detectors is installed or the detector is busy
   (Retry-After). Results are cached per file in the store (as private as the upload, expire with it);
   detection runs at most ``HEYP_DETECT_CONCURRENCY`` (default 1) at a time per process.

2. Job tools (``POST /api/jobs``; progress over SSE; results are chainable files):

   * ``img.upscale`` (lane heavy) — the ``upscale`` op per image, original format by default.
   * ``img.remove_background`` (lane heavy) — the ``background`` op (engine auto/object/person), PNG by default.
   * ``img.blur`` (lane light) — ``blur_regions`` with the detector's pre-selected faces/plates per image
     (batch "blur faces"), plus optional fixed regions.

   They run the same pipeline ops as ``/api/process``, so results are identical; outputs are named
   ``<stem>_<tool>.<ext>``.
"""
from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core import registry
from app.imaging import blur, face, io as imgio, matting, plates, segment, upscale
from app.imaging.opreg import Color, FeatureUnavailable
from app.imaging.ops_ext.ai import BlurRegion
from app.imaging.ortsession import ModelUnavailable
from app.schemas import OutputSpec

WHAT = ("faces", "plates")
SLOT_WAIT_S = 30.0
CACHE_VERSION = 1  # bump when detector settings change, so cached answers are recomputed

router = APIRouter(prefix="/detect", tags=["heyp-ai"])


def _concurrency() -> int:
    try:
        return max(1, min(8, int(os.environ.get("HEYP_DETECT_CONCURRENCY", "1"))))
    except ValueError:
        return 1


_slots = threading.BoundedSemaphore(_concurrency())


def features() -> dict[str, bool]:
    """Model availability for ``/api/config`` features and ``/api/health`` (cheap: no model is loaded)."""
    def safe(fn) -> bool:
        try:
            return bool(fn())
        except Exception:
            return False

    return {"upscale": safe(upscale.available), "matting": safe(matting.available), "plates": safe(plates.available),
            "faces": safe(face.available), "person_segmentation": safe(segment.available)}


def parse_what(raw: str | None) -> list[str]:
    """``"faces,plates"`` → ["faces", "plates"] (order kept, duplicates dropped); ValueError when invalid."""
    items = [p.strip().lower() for p in ("faces,plates" if raw is None else raw).split(",") if p.strip()]
    if not items:
        raise ValueError("what must name at least one of: faces, plates")
    unknown = [p for p in items if p not in WHAT]
    if unknown:
        raise ValueError(f"unknown detector {unknown[0][:20]!r} (use faces, plates)")
    return list(dict.fromkeys(items))


def _status_of(exc: BaseException) -> tuple[int, str] | None:
    status = getattr(exc, "status", None)
    if isinstance(status, int) and 400 <= status <= 599:
        return status, str(getattr(exc, "detail", None) or exc)
    if isinstance(exc, MemoryError):
        return 413, "Image too large to analyse"
    return None


def detect_regions(request: Request, file_id: str, what: list[str], level: str) -> dict[str, Any]:
    """Load the upload, run the detectors (or reuse the cached answer) and build the response body."""
    store = request.app.state.store
    try:
        stored = store.get(file_id)
    except Exception as exc:
        mapped = _status_of(exc)
        if mapped:
            raise HTTPException(*mapped) from None
        raise
    if getattr(stored, "kind", "image") != "image":
        raise HTTPException(422, "Face and plate detection works on images only")
    aux = f"detect-v{CACHE_VERSION}-{'-'.join(what)}-{level}.json"
    read_aux = getattr(store, "read_aux", None)
    cached = read_aux(file_id, aux) if read_aux else None
    if cached:
        try:
            return json.loads(cached)
        except ValueError:
            pass
    _rate_limit(request)
    if not _slots.acquire(timeout=SLOT_WAIT_S):
        raise HTTPException(503, "The detector is busy, try again in a moment", headers={"Retry-After": "5"})
    try:
        try:
            loaded = store.load(file_id)
            regions, warnings = blur.suggest_regions(loaded.image, what, level=level)
        except ModelUnavailable as exc:
            raise HTTPException(503, f"Detection is unavailable: {exc}") from None
        except HTTPException:
            raise
        except Exception as exc:
            mapped = _status_of(exc)
            if mapped:
                raise HTTPException(*mapped) from None
            raise
    finally:
        _slots.release()
    width, height = loaded.image.size
    unavailable = [w for w in what if (w == "faces" and not face.available()) or (w == "plates" and not plates.available())]
    body = {
        "file_id": file_id, "width": width, "height": height, "what": what, "level": level,
        "regions": [r.to_dict() for r in regions],
        "counts": {w: sum(1 for r in regions if r.kind == w[:-1]) for w in what},
        "unavailable": unavailable, "warnings": warnings,
    }
    write_aux = getattr(store, "write_aux", None)
    if write_aux and not unavailable:
        write_aux(file_id, aux, json.dumps(body, separators=(",", ":")).encode())
    return body


def _rate_limit(request: Request) -> None:
    """Hosted mode: detection counts against the per-client job budget (no-op when limits are off)."""
    try:
        from app.core.limits import client_info, get_limiter
    except ImportError:
        return
    limiter = get_limiter(request.app)
    settings = getattr(request.app.state, "settings", None)
    if limiter is None or settings is None or not getattr(limiter, "enabled", False):
        return
    try:
        limiter.check("jobs", client_info(request, settings))
    except Exception as exc:
        mapped = _status_of(exc)
        if mapped is None:
            raise
        retry = getattr(exc, "retry_after", None)
        raise HTTPException(mapped[0], mapped[1],
                            headers={"Retry-After": str(int(retry))} if retry else None) from None


FileIdPath = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$", description="upload id")]


@router.get("/{file_id}")
def detect(request: Request, file_id: FileIdPath,
           what: Annotated[str, Query(max_length=40, description="comma list: faces, plates")] = "faces,plates",
           level: Annotated[str, Query(max_length=20, description="low | recommended | high")] = "recommended",
           ) -> JSONResponse:
    """Suggested blur regions for an uploaded image (faces via YuNet, licence plates via YOLOv9-t)."""
    try:
        detectors = parse_what(what)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    level = level.strip().lower()
    if level not in blur.LEVELS:
        raise HTTPException(422, f"level must be one of {', '.join(blur.LEVELS)}")
    body = detect_regions(request, file_id, detectors, level)
    return JSONResponse(body, headers={"Cache-Control": "no-store"})



# ------------------------------------------------------------------ job tools (POST /api/jobs)
OutFormat = Literal["original", "jpeg", "png", "webp"]
_FORMAT_LABELS = {"original": "Same as input", "jpeg": "JPG", "png": "PNG", "webp": "WebP"}
_LOSSY = ("original", "jpeg", "webp")


def _ui(**hints: Any) -> dict[str, Any]:
    return {"ui": hints}


class _Options(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class UpscaleOptions(_Options):
    """Options of Upscale image (``img.upscale``)."""

    factor: Literal[2, 4] = Field(2, title="Enlarge", json_schema_extra=_ui(widget="segmented",
                                                                           labels={"2": "2×", "4": "4×"}))
    model: Literal["balanced", "strong"] = Field(
        "balanced", title="Look",
        description="Natural keeps a little texture; Smooth removes more noise and JPEG blocks.",
        json_schema_extra=_ui(widget="segmented", labels={"balanced": "Natural", "strong": "Smooth"}))
    format: OutFormat = Field("original", title="Format",
                              json_schema_extra=_ui(widget="segmented", labels=_FORMAT_LABELS))
    quality: int = Field(92, ge=1, le=100, title="Quality",
                         json_schema_extra=_ui(widget="slider", show_if={"field": "format", "in": list(_LOSSY)}))


class RemoveBackgroundOptions(_Options):
    """Options of Remove background (``img.remove_background``)."""

    engine: Literal["auto", "object", "person"] = Field(
        "auto", title="Subject",
        description="Any subject works for people, pets and products; People is faster and tuned for portraits.",
        json_schema_extra=_ui(widget="segmented", labels={"auto": "Any subject", "object": "Object",
                                                          "person": "People"}))
    mode: Literal["remove", "replace"] = Field(
        "remove", title="Background",
        json_schema_extra=_ui(widget="segmented", labels={"remove": "Transparent", "replace": "Colour"}))
    color: Color = Field("#ffffff", title="Colour",
                         json_schema_extra=_ui(widget="color", show_if={"field": "mode", "equals": "replace"}))
    format: Literal["auto", "png", "webp", "jpeg"] = Field(
        "auto", title="Format", description="Automatic: PNG for a transparent background, else the input's format.",
        json_schema_extra=_ui(widget="segmented", labels={"auto": "Automatic", "png": "PNG", "webp": "WebP",
                                                          "jpeg": "JPG"}))


class BlurOptions(_Options):
    """Options of Blur faces / plates (``img.blur``)."""

    detect: list[Literal["faces", "plates"]] = Field(
        ["faces"], max_length=2, title="Find and blur",
        json_schema_extra=_ui(widget="multi", labels={"faces": "Faces", "plates": "Licence plates"}))
    level: Literal["low", "recommended", "high"] = Field(
        "recommended", title="Search",
        description="Thorough also looks for small faces and distant plates; Fast finds only clear ones.",
        json_schema_extra=_ui(widget="segmented", labels={"low": "Fast", "recommended": "Thorough",
                                                          "high": "Everything"}))
    regions: list[BlurRegion] = Field([], max_length=blur.MAX_REGIONS, title="Areas",
                                      json_schema_extra=_ui(widget="canvas"))
    mode: Literal["blur", "pixelate", "solid"] = Field(
        "blur", title="Style", json_schema_extra=_ui(widget="segmented", labels={"blur": "Blur",
                                                                                 "pixelate": "Pixelate",
                                                                                 "solid": "Cover"}))
    strength: float = Field(0.6, ge=0.1, le=1, title="Strength",
                            json_schema_extra=_ui(widget="slider", step=0.05,
                                                  show_if={"field": "mode", "not_in": ["solid"]}))
    color: Color = Field("#000000", title="Colour",
                         json_schema_extra=_ui(widget="color", show_if={"field": "mode", "equals": "solid"}))
    format: OutFormat = Field("original", title="Format",
                              json_schema_extra=_ui(widget="segmented", labels=_FORMAT_LABELS))

    @model_validator(mode="after")
    def _something_to_do(self) -> BlurOptions:
        if not self.detect and not self.regions:
            raise ValueError("choose what to blur: faces, licence plates or areas")
        return self


def _output_spec(fmt: str, quality: int) -> OutputSpec:
    """Encoder settings for a result; ``original`` is resolved by the pipeline exactly as in /api/process
    (the input's format when it can be written, PNG when an alpha-less format would lose transparency)."""
    return OutputSpec(format=fmt, quality=quality)


def _run_ops(jctx: Any, index: int, inp: Any, ops: list[dict], output: Callable[[Any], OutputSpec],
             message: str) -> dict[str, Any]:
    """Run ``ops`` on input ``index`` through the pipeline (as /api/process does) and store the result."""
    from app.imaging import pipeline

    total = max(1, len(jctx.inputs))
    suffix = f" ({index + 1} of {total})" if total > 1 else ""

    def progress(frac: float, msg: str = "") -> None:
        jctx.progress((index + max(0.0, min(1.0, frac)) * 0.95) / total, f"{msg or message}{suffix}")

    jctx.check_cancel()
    progress(0.0)
    loaded = jctx.load_image(inp.id)
    spec = output(loaded)
    animation = None
    if pipeline.wants_animation(loaded, spec):  # GIF/WebP/APNG keep every frame when all ops support it
        animation = pipeline.load_animation(str(inp.path), check_cancel=jctx.check_cancel)
    store = getattr(jctx, "store", None)
    pctx = pipeline.run(loaded, ops, cache_key=inp.id, max_pixels=jctx.settings.max_pixels,
                        alive=(lambda: store.exists(inp.id)) if store is not None else None,
                        animation=animation, on_progress=progress, cancel_check=jctx.check_cancel)
    jctx.check_cancel()
    progress(0.97, "Saving")
    data, fmt, info = pipeline.render(pctx, spec)
    for w in info.get("warnings") or ():
        jctx.warn(f"{inp.name}: {w}" if total > 1 else w)
    # <stem>_<tool>.<ext>; add_output makes it unique within the job
    name = f"{inp.stem}_{jctx.tool.id.split('.', 1)[1]}.{imgio.ext_for(fmt)}"
    saved = jctx.add_output(data, name, imgio.mime_for(fmt), kind="image")
    return {"input": inp.id, "output": saved.get("id"), "width": info["width"], "height": info["height"],
            "format": fmt}


def run_upscale(jctx: Any, opts: UpscaleOptions) -> None:
    ops = [{"op": "upscale", "factor": opts.factor, "model": opts.model}]
    results = []
    for i, inp in enumerate(jctx.inputs):
        results.append(_run_ops(jctx, i, inp, ops,
                                lambda _c: _output_spec(opts.format, opts.quality),
                                f"Upscaling {opts.factor}×"))
    jctx.data["images"] = results


def run_remove_background(jctx: Any, opts: RemoveBackgroundOptions) -> None:
    op: dict[str, Any] = {"op": "background", "mode": opts.mode, "engine": opts.engine}
    if opts.mode == "replace":
        op["color"] = opts.color

    def output(loaded: Any) -> OutputSpec:
        fmt = opts.format
        if fmt == "auto":
            fmt = "png" if opts.mode == "remove" else (loaded.format if loaded.format in ("jpeg", "png", "webp")
                                                       else "png")
        if fmt == "jpeg" and opts.mode == "remove":
            jctx.warn("JPG can't be transparent, so the background was filled with white; choose PNG or WebP "
                      "to keep it transparent")
        return OutputSpec(format=fmt, quality=92)

    jctx.data["images"] = [_run_ops(jctx, i, inp, [op], output, "Removing the background")
                           for i, inp in enumerate(jctx.inputs)]


def run_blur(jctx: Any, opts: BlurOptions) -> None:
    results = []
    total = max(1, len(jctx.inputs))
    for i, inp in enumerate(jctx.inputs):
        regions = [r.model_dump() for r in opts.regions]
        found: dict[str, int] = {}
        if opts.detect:
            jctx.check_cancel()
            jctx.progress(i / total, f"Looking for {' and '.join(opts.detect)}"
                          + (f" in {i + 1} of {total}" if total > 1 else ""))
            loaded = jctx.load_image(inp.id)
            image = loaded.image
            if getattr(loaded, "frames", 1) > 1:
                jctx.warn("In animations, faces and plates are looked for in the first frame only")
            try:
                suggestions, warnings = blur.suggest_regions(image, opts.detect, level=opts.level,
                                                             check_cancel=jctx.check_cancel)
            except ModelUnavailable as exc:
                raise FeatureUnavailable(f"Detection is unavailable: {exc}") from None
            for w in warnings:
                jctx.warn(w)
            picked = [s for s in suggestions if s.selected]
            room = blur.MAX_REGIONS - len(regions)
            if len(picked) > room:
                jctx.warn(f"{inp.name}: only the {room} most confident of {len(picked)} detections were blurred")
                picked = picked[:max(0, room)]
            regions += [{"x": s.x, "y": s.y, "w": s.w, "h": s.h, "shape": s.shape} for s in picked]
            for kind in opts.detect:
                found[kind] = sum(1 for s in picked if s.kind == kind[:-1])
            if not picked and not opts.regions:
                jctx.warn(f"Nothing to blur was found in {inp.name}" if total > 1 else "Nothing to blur was found")
        op: dict[str, Any] = {"op": "blur_regions", "regions": regions, "mode": opts.mode,
                              "strength": opts.strength, "color": opts.color}
        summary = _run_ops(jctx, i, inp, [op],
                           lambda _c: _output_spec(opts.format, 92),
                           "Blurring")
        summary["found"] = found
        summary["regions"] = len(regions)
        results.append(summary)
    jctx.data["images"] = results


def _check(fn: Callable[[], bool], reason: str) -> Callable[[], tuple[bool, str]]:
    def check() -> tuple[bool, str]:
        try:
            ok = bool(fn())
        except Exception:
            ok = False
        return (True, "") if ok else (False, reason)

    return check


def _remove_background_available() -> bool:
    return matting.available() or segment.available()


def _detectors_available() -> bool:
    return face.available() or plates.available()


TOOLS = (
    registry.Tool(
        id="img.upscale", title="Upscale image", category="Edit",
        summary="Enlarge photos 2× or 4× with an AI model that adds real detail instead of blur.",
        inputs=("image",), options=UpscaleOptions, run=run_upscale, max_files=3, lane="heavy", timeout_s=1200,
        limits={"max_files": 3, "memory_mb": 1200},
        outputs="One image per input, <name>_upscale.<ext>, in the input's format unless another is chosen.",
        check=_check(lambda: upscale.available("balanced") and upscale.available("strong"),
                     "The upscaling models are not installed on this server"),
    ),
    registry.Tool(
        id="img.remove_background", title="Remove background", category="Edit",
        summary="Cut out people, pets or products and make the background transparent or a plain colour.",
        inputs=("image",), options=RemoveBackgroundOptions, run=run_remove_background, max_files=20,
        lane="heavy", timeout_s=900, limits={"max_files": 20, "memory_mb": 1900},
        outputs="One image per input, <name>_remove_background.png (or the chosen format).",
        check=_check(_remove_background_available, "No background-removal model is installed on this server"),
    ),
    registry.Tool(
        id="img.blur", title="Blur faces", category="Edit",
        summary="Find faces and licence plates and blur, pixelate or cover them — or any area you mark.",
        inputs=("image",), options=BlurOptions, run=run_blur, max_files=30, lane="light", timeout_s=600,
        limits={"max_files": 30},
        outputs="One image per input, <name>_blur.<ext>, in the input's format unless another is chosen.",
        check=_check(_detectors_available, "Face and plate detection are not installed on this server"),
    ),
)

for _tool in TOOLS:
    registry.register(_tool)


__all__ = ["router", "features", "parse_what", "detect_regions", "UpscaleOptions", "RemoveBackgroundOptions",
           "BlurOptions", "TOOLS"]
