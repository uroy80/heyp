"""Run a list of ops over a loaded image and encode the result (PLAN.md §4).

``run`` applies ops in order to the EXIF-oriented image and tracks the pipeline DPI; ``render``
encodes with the output options; ``plan`` estimates a job's cost before any pixel is touched.
Ops are dispatched through the plug-in registry (``app.imaging.opreg``); the v1 ops are registered
below as *fallbacks* until ``ops_ext/core.py`` takes them over with identical JSON.

Animations: given ``animation=`` (see :func:`load_animation`, which enforces the frame-bomb guard),
every op is applied per frame when all of them have a ``frame_apply``; otherwise the first frame is
used with a warning. ``render`` writes GIF/WebP animations.

Errors: ValueError → 422, PipelineError carries its own status (413 too many pixels, 503 feature
unavailable), io.ImageError carries 413/415.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from io import BytesIO
from typing import Any

import numpy as np
from PIL import Image
from pydantic import BaseModel, ValidationError

from app import schemas
from app.config import get_settings
from app.imaging import compress, io, opreg, ops as imops, presets
from app.imaging.opreg import (LRU as _LRU, Ctx, FeatureUnavailable, OpSpec, PipelineError,  # noqa: F401
                               check_pixels as _check_pixels, memo_key as _opreg_memo_key)
from app.schemas import OUTPUT_FORMATS, OutputSpec, ratio_value

_UNIT_PER_INCH = {"in": 1.0, "cm": 2.54, "mm": 25.4}
# Formats whose files carry a resolution we write (JPEG JFIF, PNG pHYs, TIFF tags, BMP, PDF page size, HEIC EXIF).
DPI_FORMATS = {"jpeg", "png", "tiff", "bmp", "pdf", "heic"}
_ALPHA_LESS = {"jpeg", "bmp"}
PDF_DEFAULT_DPI = 300  # io writes PDF pages at 300 dpi when the pipeline has none
SHEET_DEFAULT_DPI = 300
SHEET_AUTO_DPI = (300, 600)  # a sheet without dpi follows the pipeline dpi, clamped to this print range
_KEEPS_GEOMETRY = frozenset({"adjust", "background"})  # ops after a passport op that keep its guides valid
MASK_CACHE_BYTES = 256 * 1024 * 1024
ANALYSIS_CACHE_BYTES = 128 * 1024 * 1024
# Caps for ops/encoders whose memory per pixel is far above the rest, so raising HEYP_MAX_PIXELS for
# scans can't make them OOM (peaks measured at 50 MP: person segmentation ≈ 33 B/px, lossless WebP
# ≈ 35, AVIF ≈ 24, GIF and adjust ≈ 11; JPEG ≈ 6).
OP_MAX_PIXELS = {"background": 50_000_000, "adjust": 100_000_000}
OP_LABELS = {"background": "Background removal", "adjust": "Adjustments"}
ENCODE_MAX_PIXELS = {"avif": 50_000_000, "gif": 100_000_000}
LOSSLESS_ENCODE_MAX_PIXELS = {"webp": 40_000_000, "avif": 50_000_000}


_analysis_cache = opreg.cache("analysis", 8, ANALYSIS_CACHE_BYTES)  # passport.analyze results
_mask_cache = opreg.cache("background-mask", 3, MASK_CACHE_BYTES)  # full-resolution person masks (float16), background op


def forget(cache_key: str) -> None:
    """Drop memoised analysis for an image (call when its upload is deleted)."""
    if cache_key:
        opreg.forget(cache_key)
        for lru in (_analysis_cache, _mask_cache):  # may be swapped out by tests
            lru.drop_prefix(cache_key + "|")


def clear_caches() -> None:
    opreg.clear_caches()
    _analysis_cache.clear()
    _mask_cache.clear()


def _memo_put(ctx: Ctx, cache: _LRU, key: str, value: Any) -> None:
    """Store, then undo if the upload was deleted meanwhile (its forget() may already have run)."""
    cache.put(key, value)
    if ctx.alive is not None and not ctx.alive():
        forget(ctx.cache_key)


# ------------------------------------------------------------------ animation
# Frame decoding/encoding lives in app/imaging/animated.py; the pipeline decides when to use it.
ANIMATED_SOURCES = frozenset({"gif", "webp", "png"})
ANIMATED_OUTPUTS = frozenset({"gif", "webp", "png"})  # png → APNG


def _animated_module() -> Any:
    try:
        from app.imaging import animated
    except ImportError as exc:  # pragma: no cover - shipped with the app
        raise FeatureUnavailable(f"Animation support is unavailable: {exc}") from None
    return animated


def still_warning(fmt: str) -> str:
    """The warning for an animated input written as a still (PNG can animate, but heyp writes APNG only from APNG)."""
    return f"Saved as a still {fmt.upper()} — only the first frame of the animation was kept"


def wants_animation(loaded: io.Loaded, output: OutputSpec | None) -> bool:
    """True when the source is animated and the requested output can hold an animation."""
    return animated_output(loaded.format, getattr(loaded, "frames", 1), output)


def animated_output(source_format: str | None, frames: int | None, output: OutputSpec | None) -> bool:
    """``wants_animation`` from an upload's recorded format and frame count (no decoding)."""
    if not frames or frames <= 1 or source_format not in ANIMATED_SOURCES:
        return False
    output = output or OutputSpec()
    if output.format == "original":
        return True  # gif → gif, webp → webp, apng → apng
    return output.format in ANIMATED_OUTPUTS and output.format != "png"  # an explicit PNG means a still


def check_animation_size(frames: int, width: int, height: int, *, max_frames: int | None = None,
                         max_pixels: int | None = None) -> None:
    """Frame-bomb guard (critic blocking #4): ≤ HEYP_MAX_ANIM_FRAMES frames and ≤ HEYP_MAX_ANIM_PIXELS
    decoded pixels across all frames — checked from the header, before decoding anything → 413.
    The limits default to the process settings; the API passes its app's."""
    settings = get_settings()
    max_frames = max_frames or settings.max_anim_frames
    max_pixels = max_pixels or settings.max_anim_pixels
    if frames > max_frames:
        raise PipelineError(413, f"The animation has {frames} frames; at most {max_frames} can be "
                                 "processed. Choose a still format such as JPEG or PNG to use the first frame")
    if frames * width * height > max_pixels:
        raise PipelineError(413, f"The animation is too large to process ({frames} frames of {width}×{height} px = "
                                 f"{frames * width * height / 1e6:.0f} MP; the limit is "
                                 f"{max_pixels / 1e6:.0f} MP). Choose a still format such as JPEG "
                                 "or PNG to use the first frame")


def load_animation(source: Any, *, check_cancel: Callable[[], None] | None = None,
                   max_frames: int | None = None, max_pixels: int | None = None) -> Any | None:
    """Every frame of an animated GIF/WebP/APNG (``animated.Animation``: frames, durations, loop), or
    None for a still image. Limits are checked before any frame is decoded (413)."""
    animated = _animated_module()
    probe = animated.probe(source)
    if probe is not None:
        check_animation_size(probe[1], probe[2], probe[3], max_frames=max_frames, max_pixels=max_pixels)
    return animated.load(source, check_cancel=check_cancel)


def _encode_animation(ctx: Ctx, fmt: str, output: OutputSpec) -> bytes:
    frames = ctx.frames or [ctx.image]
    durations = list(ctx.durations or [100] * len(frames))
    return _animated_module().encode(frames, durations, ctx.loop, fmt, quality=output.quality,
                                     lossless=output.lossless, background=output.background)


# ------------------------------------------------------------------ run
def run(loaded: io.Loaded, ops: Sequence[BaseModel | dict], *, cache_key: str = "",
        max_pixels: int | None = None, alive: Callable[[], bool] | None = None,
        animation: Any | None = None, on_progress: Callable[[float, str], None] | None = None,
        cancel_check: Callable[[], None] | None = None,
        resolve_file: Callable[[str], Any] | None = None, max_anim_pixels: int | None = None) -> Ctx:
    """Apply ``ops`` in order.

    ``cache_key`` (the upload id) enables analysis memoisation; ``alive()`` reports whether that
    upload still exists, so a job finishing after a DELETE leaves nothing cached. ``max_pixels``
    defaults to HEYP_MAX_PIXELS. ``animation`` (from :func:`load_animation`) makes the ops run per
    frame when every op supports it. ``on_progress``/``cancel_check`` come from a job.
    """
    op_list = _coerce_ops(ops)
    _ensure_fallbacks()
    settings = get_settings()
    limit = max_pixels or settings.max_pixels
    ctx = Ctx(image=loaded.image, dpi=loaded.dpi, exif=loaded.exif, icc_profile=loaded.icc_profile,
              source_format=loaded.format, cache_key=cache_key, max_pixels=limit, alive=alive,
              on_progress=on_progress, cancel_check=cancel_check, resolve_file=resolve_file)
    if ctx.image.width * ctx.image.height > limit:
        raise PipelineError(413, f"Image is {ctx.image.width * ctx.image.height / 1e6:.0f} MP, "
                                 f"above the {limit / 1e6:.0f} MP limit")
    specs = [_spec(op.op) for op in op_list]
    if animation is not None and len(getattr(animation, "frames", ())) > 1:
        missing = [spec.title for spec in specs if spec.frame_apply is None]
        if missing:
            ctx.warn(f"{missing[0]} can't be applied to each frame of an animation — only the first frame was kept")
        else:
            ctx.frames, ctx.durations, ctx.loop = list(animation.frames), list(animation.durations), animation.loop
            ctx.image = ctx.frames[0]
    for index, (op, spec) in enumerate(zip(op_list, specs)):
        ctx.check_cancel()
        cap = OP_MAX_PIXELS.get(op.op) or spec.max_pixels
        if cap and ctx.image.width * ctx.image.height > cap:
            label = OP_LABELS.get(op.op) or spec.title
            raise PipelineError(413, f"{label} works on images up to {cap / 1e6:.0f} MP (this one is "
                                     f"{ctx.image.width * ctx.image.height / 1e6:.0f} MP); crop or downsize it first")
        if ctx.frames is not None:
            _apply_frames(ctx, op, spec, max_anim_pixels or settings.max_anim_pixels)
        else:
            spec.apply(ctx, op, op_list[:index])
        _check_pixels(ctx.image.width, ctx.image.height, limit, op.op)
        if op.op != "passport" and op.op not in _KEEPS_GEOMETRY and (spec.geometry or op.op in _V1_GEOMETRY):
            ctx.meta.pop("passport_guides", None)  # its pixel positions no longer match the image
    return ctx


def _apply_frames(ctx: Ctx, op: BaseModel, spec: OpSpec, max_total: int) -> None:
    frames = ctx.frames or []
    out: list[Image.Image] = []
    for index, frame in enumerate(frames):
        ctx.check_cancel()
        ctx.frame_index = index
        result = spec.frame_apply(ctx, op, frame)  # type: ignore[misc]
        if out and result.size != out[0].size:
            raise PipelineError(422, f"{spec.title} gave the animation frames different sizes")
        out.append(result)
        if (index + 1) * result.width * result.height > max_total:
            raise PipelineError(413, f"{spec.title}: the animation would exceed {max_total / 1e6:.0f} MP in total")
    ctx.frame_index = 0
    ctx.frames = out
    ctx.image = out[0]


def passport_guides(ctx: Ctx) -> dict | None:
    """Passport measurements + preset bands in output px (PLAN.md §3 PreviewResponse), or None.

    Only set when the last geometry-defining op was a passport op, so its coordinates match
    ``ctx.image`` (and the preview's full-resolution width/height).
    """
    return ctx.meta.get("passport_guides")


def _coerce_ops(ops: Sequence[BaseModel | dict]) -> list[BaseModel]:
    if all(isinstance(op, BaseModel) for op in ops):
        return list(ops)  # type: ignore[arg-type]
    try:
        raw = [op.model_dump() if isinstance(op, BaseModel) else op for op in ops]
        return list(schemas.OPS_ADAPTER.validate_python(raw))
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", ()))
        raise ValueError(f"invalid op ({loc}): {first.get('msg', 'invalid')}") from None


_memo_key = _opreg_memo_key


# ------------------------------------------------------------------ op handlers
def _rotate(ctx: Ctx, op: Any, _prefix: Sequence[BaseModel]) -> None:
    if op.expand and op.angle % 90:
        rad = math.radians(op.angle)
        w, h = ctx.image.size
        bw = abs(w * math.cos(rad)) + abs(h * math.sin(rad))
        bh = abs(w * math.sin(rad)) + abs(h * math.cos(rad))
        _check_pixels(bw, bh, ctx.max_pixels, "rotate")
    ctx.image = imops.rotate(ctx.image, op.angle, expand=op.expand, fill=op.fill)


def _flip(ctx: Ctx, op: Any, _prefix: Sequence[BaseModel]) -> None:
    ctx.image = imops.flip(ctx.image, op.direction)


def _crop(ctx: Ctx, op: Any, _prefix: Sequence[BaseModel]) -> None:
    ctx.image = imops.crop(ctx.image, op.x, op.y, op.width, op.height, unit=op.unit)


def _crop_aspect(ctx: Ctx, op: Any, _prefix: Sequence[BaseModel]) -> None:
    faces = None
    if op.anchor == "face":
        faces = _detect_faces(ctx.image)
        if not faces:
            ctx.warn("No face found — used smart crop instead")
    ctx.image = imops.crop_aspect(ctx.image, op.ratio, anchor=op.anchor, faces=faces)


def _detect_faces(img: Image.Image) -> list:
    try:
        from app.imaging import face
    except ImportError:
        return []
    return face.detect(img)


def _trim(ctx: Ctx, op: Any, _prefix: Sequence[BaseModel]) -> None:
    ctx.image = imops.trim(ctx.image, tolerance=op.tolerance, padding=op.padding)


def _resize_estimate(size: tuple[int, int], op: Any) -> tuple[float, float]:
    """Upper bound of the resize result, used to refuse absurd sizes before allocating them."""
    w, h = size

    def px(value: float | None) -> float | None:
        if value is None:
            return None
        return value if op.unit == "px" else value / _UNIT_PER_INCH[op.unit] * op.dpi

    tw, th, ts = px(op.width), px(op.height), px(op.size)
    if op.mode in ("fill", "pad", "exact") and tw and th:
        out = (tw, th)
    else:
        if op.mode == "scale" and op.scale:
            f = op.scale / 100.0
        elif op.mode == "long_edge" and ts:
            f = ts / max(w, h)
        elif op.mode == "short_edge" and ts:
            f = ts / min(w, h)
        elif op.mode == "megapixels" and op.megapixels:
            f = math.sqrt(op.megapixels * 1e6 / (w * h))
        elif op.mode == "fit":
            f = min(x for x in ((tw / w) if tw else None, (th / h) if th else None) if x is not None)
        else:
            f = 1.0
        out = (w * f, h * f)
    if not op.upscale:
        out = (min(out[0], w), min(out[1], h)) if op.mode != "pad" else out
    return out


def _resize(ctx: Ctx, op: Any, _prefix: Sequence[BaseModel]) -> None:
    est_w, est_h = _resize_estimate(ctx.image.size, op)
    _check_pixels(est_w, est_h, ctx.max_pixels, "resize")
    ctx.image = imops.resize(
        ctx.image, op.mode, width=op.width, height=op.height, scale=op.scale, size=op.size,
        megapixels=op.megapixels, unit=op.unit, dpi=op.dpi, upscale=op.upscale, resample=op.resample,
        background=op.background,
    )
    if op.unit != "px":
        ctx.dpi = (float(op.dpi), float(op.dpi))


def _pad(ctx: Ctx, op: Any, _prefix: Sequence[BaseModel]) -> None:
    w, h = ctx.image.size
    _check_pixels(w + op.left + op.right, h + op.top + op.bottom, ctx.max_pixels, "pad")
    ctx.image = imops.pad(ctx.image, top=op.top, right=op.right, bottom=op.bottom, left=op.left, color=op.color)


def _adjust(ctx: Ctx, op: Any, _prefix: Sequence[BaseModel]) -> None:
    ctx.image = imops.adjust(ctx.image, brightness=op.brightness, contrast=op.contrast,
                             saturation=op.saturation, sharpness=op.sharpness,
                             grayscale=op.grayscale, auto_contrast=op.auto_contrast)


def _segment_module():
    try:
        from app.imaging import segment
    except ImportError:
        segment = None
    if segment is None or not segment.available():
        raise FeatureUnavailable("Background removal is unavailable: the person-segmentation model is not installed")
    return segment


def _background(ctx: Ctx, op: Any, prefix: Sequence[BaseModel]) -> None:
    segment = _segment_module()
    key = _memo_key(ctx, "mask", prefix)
    mask = _mask_cache.get(key) if key else None
    if mask is None or mask.shape != (ctx.image.height, ctx.image.width):
        try:
            mask = segment.person_mask(ctx.image)
        except RuntimeError as exc:  # model failed to load
            raise FeatureUnavailable(f"Background removal is unavailable: {exc}") from None
        if key:
            _memo_put(ctx, _mask_cache, key, mask.astype(np.float16))
    color = "transparent" if op.mode == "remove" else op.color
    ctx.image = segment.composite(ctx.image, np.asarray(mask, dtype=np.float32), color)


def _passport_preset(op: Any) -> presets.PassportPreset:
    if op.preset == "custom":
        assert op.custom is not None  # enforced by the schema
        c = op.custom
        return presets.custom_preset(c.width_mm, c.height_mm, dpi=c.dpi, head_min=c.head_min,
                                     head_max=c.head_max, eye_min=c.eye_min, eye_max=c.eye_max,
                                     background=c.background, pixel_width=c.pixel_width,
                                     pixel_height=c.pixel_height)
    try:
        return presets.get_preset(op.preset)
    except KeyError:
        raise ValueError(f"unknown passport preset {op.preset!r}") from None


def _passport_module():
    try:
        from app.imaging import passport
    except ImportError as exc:
        raise FeatureUnavailable(f"Passport tools are unavailable: {exc}") from None
    return passport


def _passport(ctx: Ctx, op: Any, prefix: Sequence[BaseModel]) -> None:
    preset = _passport_preset(op)
    out_w, out_h = preset.output_size()
    _check_pixels(out_w, out_h, ctx.max_pixels, "passport")
    passport = _passport_module()
    key = _memo_key(ctx, "passport", prefix)
    analysis = _analysis_cache.get(key) if key else None
    if analysis is None:
        analysis = passport.analyze(ctx.image)
        if key:
            _memo_put(ctx, _analysis_cache, key, analysis)
    image, info = passport.make(
        ctx.image, preset, analysis=analysis, background=op.background,
        replace_background=op.replace_background, face_index=op.face_index, zoom=op.zoom,
        offset_x=op.offset_x, offset_y=op.offset_y, level=op.level,
    )
    ctx.image = image
    dpi = info.get("dpi") or preset.dpi
    ctx.dpi = (float(dpi), float(dpi))
    for message in info.get("warnings") or []:
        ctx.warn(message)
    ctx.meta["passport"] = {k: v for k, v in info.items() if k != "warnings"}
    ctx.meta["passport_preset"] = preset.id
    ctx.meta["passport_guides"] = _guides(preset, info, image.size)


def _guides(preset: presets.PassportPreset, info: dict, size: tuple[int, int]) -> dict:
    """The preview's ``passport`` object: measurements, guide lines (output px) and preset bands,
    plus ``source`` (the frame on the image entering the op, for the UI's context view).

    ``lines``/``bands`` come from passport.make's info when it reports them, else from the
    face box and the preset.
    """
    face_found = bool(info.get("face_found"))
    lines = info.get("lines") if face_found else None
    box = info.get("face_box_out")
    if lines is None and face_found and isinstance(box, dict) \
            and all(box.get(k) is not None for k in ("crown_y", "chin_y", "eye_y")):
        lines = {"crown": box["crown_y"], "chin": box["chin_y"], "eye": box["eye_y"]}
    head = preset.head_max > 0
    bands = info.get("bands") or {
        "head_min": preset.head_min if head else None, "head_max": preset.head_max if head else None,
        "eye_min": preset.eye_min, "eye_max": preset.eye_max, "top_min": preset.top_min, "top_max": preset.top_max,
    }
    return {
        "preset": preset.id,
        "width": size[0],
        "height": size[1],
        "head_ratio": _float(info.get("head_ratio")),
        "eye_from_bottom": _float(info.get("eye_from_bottom")),
        "face_found": face_found,
        "roll_deg": _float(info.get("roll_deg")),
        "lines": {k: float(lines[k]) for k in ("crown", "chin", "eye")} if lines else None,
        # top of the hair (output px; differs from the crown line when the preset excludes hair): the UI
        # predicts where a head-size change re-frames the photo (PLAN §5.3 step 3)
        "hair_top": _float(box.get("top_y")) if lines and isinstance(box, dict) else None,
        "bands": {k: _float(bands.get(k)) for k in ("head_min", "head_max", "eye_min", "eye_max", "top_min", "top_max")},
        "background_edit_allowed": bool(info.get("background_edit_allowed",
                                                 getattr(preset, "background_edit_allowed", True))),
        "source": _source_frame(info.get("source_frame")),
    }


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def _source_frame(value: Any) -> dict | None:
    """passport.make's ``source_frame``: where the frame sits on the image entering the passport op
    (its full-resolution size, frame centre/size in its px, clockwise angle), or None."""
    if not isinstance(value, dict):
        return None
    try:
        center, size = [float(v) for v in value["center"]], [float(v) for v in value["size"]]
        if len(center) != 2 or len(size) != 2:
            return None
        return {"width": int(value["width"]), "height": int(value["height"]),
                "center": center, "size": size, "angle": float(value.get("angle") or 0.0)}
    except (KeyError, TypeError, ValueError):
        return None


def sheet_dpi(op: Any, pipeline_dpi: tuple[float, float] | None) -> int:
    """Explicit ``op.dpi``, else the pipeline dpi (e.g. a 400-dpi preset) clamped to 300–600, else 300."""
    if op.dpi:
        return op.dpi
    if pipeline_dpi and pipeline_dpi[0] > 0:
        lo, hi = SHEET_AUTO_DPI
        return int(min(hi, max(lo, round(pipeline_dpi[0]))))
    return SHEET_DEFAULT_DPI


def _sheet(ctx: Ctx, op: Any, _prefix: Sequence[BaseModel]) -> None:
    paper = presets.PAPERS.get(op.paper)
    if paper is None:
        raise ValueError(f"unknown paper {op.paper!r} (choose from {', '.join(presets.PAPERS)})")
    dpi = sheet_dpi(op, ctx.dpi)
    _check_pixels(paper.width_mm / 25.4 * dpi, paper.height_mm / 25.4 * dpi, ctx.max_pixels, "sheet")
    image, info = _passport_module().sheet(
        ctx.image, paper=op.paper, orientation=op.orientation, photo_width_mm=op.photo_width_mm,
        photo_height_mm=op.photo_height_mm, dpi=dpi, gap_mm=op.gap_mm, margin_mm=op.margin_mm,
        cut_lines=op.cut_lines, copies=op.copies, src_dpi=ctx.dpi[0] if ctx.dpi else None,
    )
    ctx.image = image
    ctx.dpi = (float(dpi), float(dpi))
    for message in info.get("warnings") or []:
        ctx.warn(message)
    ctx.meta["sheet"] = {k: v for k, v in info.items() if k != "warnings"}
    ctx.meta["sheet_paper"] = paper.id


# ------------------------------------------------------------------ plan (admission and work budget)
# Reference costs measured on one core (seconds per megapixel) and peak bytes per pixel above the
# Pillow images themselves (4 B/px each). Estimates only: they size admission, not correctness.
DECODE_COST = 0.03
OP_COST = {"rotate": 0.04, "flip": 0.005, "crop": 0.002, "crop_aspect": 0.01, "trim": 0.01, "resize": 0.015,
           "pad": 0.005, "adjust": 0.02, "background": 0.04, "passport": 0.02, "sheet": 0.01}
OP_FIXED_COST = {"background": 0.6, "passport": 1.0, "crop_aspect": 0.2}
ENCODE_COST = {"jpeg": 0.03, "png": 0.16, "webp": 0.28, "avif": 0.08, "heic": 0.34, "tiff": 0.035,
               "bmp": 0.005, "gif": 0.02, "pdf": 0.03}
LOSSLESS_ENCODE_COST = {"webp": 0.5, "avif": 0.3}
TARGET_SIZE_FACTOR = 5  # a max_kb/min_kb search costs about this many full-size encodes
OP_EXTRA_BYTES = {"background": 29, "adjust": 7, "passport": 6, "rotate": 3, "resize": 3, "crop_aspect": 2,
                  "trim": 2}
ENCODE_EXTRA_BYTES = {"jpeg": 3, "png": 3, "webp": 7, "avif": 20, "heic": 3, "tiff": 8, "bmp": 4, "gif": 8, "pdf": 4}
LOSSLESS_EXTRA_BYTES = {"webp": 32, "avif": 24}
_MP = 1e6


@dataclass(frozen=True)
class Plan:
    work: float       # estimated CPU seconds on a reference core
    peak_bytes: int   # estimated peak memory of the job
    peak_pixels: int  # largest image the job holds


def plan(size: tuple[int, int], ops: Sequence[BaseModel | dict], output: OutputSpec | None = None, *,
         source_format: str = "jpeg", dpi: float | None = None, decode: bool = True, frames: int = 1) -> Plan:
    """Estimate one file's job (decode → ops → encode) from its size alone, before running it.

    Sizes are upper bounds per op (crop keeps at most the input, passport/sheet use their fixed
    frames). ``frames`` > 1 plans an animation (every frame decoded, processed and encoded; all kept
    in memory). Raises ValueError for ops that can't run at all (unknown preset or paper).
    """
    output = output or OutputSpec()
    _ensure_fallbacks()
    n = max(1, int(frames))
    w, h = (max(1.0, float(v)) for v in size)
    cur = src = w * h
    work = DECODE_COST * src * n / _MP if decode else 0.0
    step = 8 * src if decode else 0.0  # decoding briefly holds two copies
    peak_px = src
    for op in _coerce_ops(ops):
        spec = _spec(op.op)
        if spec.plan_size is not None:
            (w, h), dpi = spec.plan_size((w, h), op, dpi)
            w, h = max(1.0, float(w)), max(1.0, float(h))
        else:
            (w, h), dpi = _planned_size((w, h), op, dpi)
        out = w * h
        if opreg.is_fallback(op.op):  # a v1 handler (the shim): the v1 measurements
            cost, fixed = OP_COST[op.op], OP_FIXED_COST.get(op.op, 0.0)
            extra_bytes, fixed_bytes = OP_EXTRA_BYTES.get(op.op, 0), 0
        else:  # a plug-in: its own estimates (e.g. BiRefNet's fixed model cost)
            cost, fixed, extra_bytes, fixed_bytes = spec.cost, spec.fixed_cost, spec.extra_bytes, spec.fixed_bytes
        work += fixed + cost * max(cur, out) * n / _MP
        step = max(step, 4 * (src + cur + out) + extra_bytes * cur + fixed_bytes)
        cur, peak_px = out, max(peak_px, out)
    fmt = output.format if output.format != "original" else (source_format or "jpeg")
    lossless = output.lossless and fmt in LOSSLESS_ENCODE_COST
    cost = LOSSLESS_ENCODE_COST[fmt] if lossless else ENCODE_COST.get(fmt, 0.1)
    if output.max_kb is not None or output.min_kb is not None:
        cost *= TARGET_SIZE_FACTOR
    work += cost * cur * n / _MP
    extra = LOSSLESS_EXTRA_BYTES[fmt] if lossless else ENCODE_EXTRA_BYTES.get(fmt, 4)
    step = max(step, 4 * src + (4 + extra) * cur)
    if n > 1:  # every source and result frame is held at once
        step += 4 * (n - 1) * (src + cur)
    return Plan(work=round(work, 3), peak_bytes=int(step), peak_pixels=int(peak_px))


def _planned_size(size: tuple[float, float], op: Any, dpi: float | None) -> tuple[tuple[float, float], float | None]:
    """Upper bound of a v1 op's output size, and the pipeline dpi after it (others keep the size)."""
    w, h = size
    name = op.op
    if name == "rotate":
        if op.angle % 90 == 0:
            return ((h, w) if op.angle % 180 else (w, h)), dpi
        if not op.expand:
            return (w, h), dpi
        rad = math.radians(op.angle)
        c, s = abs(math.cos(rad)), abs(math.sin(rad))
        return (w * c + h * s, w * s + h * c), dpi
    if name == "crop":
        if op.unit == "fraction":
            return (max(1.0, w * op.width), max(1.0, h * op.height)), dpi
        return (max(1.0, min(w, op.width)), max(1.0, min(h, op.height))), dpi
    if name == "crop_aspect":
        r = ratio_value(op.ratio)
        return ((h * r, h) if w / h > r else (w, w / r)), dpi
    if name == "trim":
        return (w + 2 * op.padding, h + 2 * op.padding), dpi
    if name == "resize":
        out = _resize_estimate((w, h), op)
        return (max(1.0, out[0]), max(1.0, out[1])), (float(op.dpi) if op.unit != "px" else dpi)
    if name == "pad":
        return (w + op.left + op.right, h + op.top + op.bottom), dpi
    if name == "passport":
        preset = _passport_preset(op)
        return tuple(float(v) for v in preset.output_size()), float(preset.dpi)  # type: ignore[return-value]
    if name == "sheet":
        paper = presets.PAPERS.get(op.paper)
        if paper is None:
            raise ValueError(f"unknown paper {op.paper!r} (choose from {', '.join(presets.PAPERS)})")
        d = sheet_dpi(op, (dpi, dpi)) if dpi else (op.dpi or SHEET_AUTO_DPI[1])  # unknown source dpi: upper bound
        return (paper.width_mm / 25.4 * d, paper.height_mm / 25.4 * d), float(d)
    return (w, h), dpi


# ------------------------------------------------------------------ v1 ops as registry fallbacks
_HANDLERS: dict[str, Callable[[Ctx, Any, Sequence[BaseModel]], None]] = {
    "rotate": _rotate, "flip": _flip, "crop": _crop, "crop_aspect": _crop_aspect, "trim": _trim,
    "resize": _resize, "pad": _pad, "adjust": _adjust, "background": _background,
    "passport": _passport, "sheet": _sheet,
}
_V1_GEOMETRY = frozenset({"rotate", "flip", "crop", "crop_aspect", "trim", "resize", "pad", "sheet"})


def _frame_rotate(ctx: Ctx, op: Any, image: Image.Image) -> Image.Image:
    return imops.rotate(image, op.angle, expand=op.expand, fill=op.fill)


def _frame_flip(ctx: Ctx, op: Any, image: Image.Image) -> Image.Image:
    return imops.flip(image, op.direction)


def _frame_crop(ctx: Ctx, op: Any, image: Image.Image) -> Image.Image:
    return imops.crop(image, op.x, op.y, op.width, op.height, unit=op.unit)


def _frame_resize(ctx: Ctx, op: Any, image: Image.Image) -> Image.Image:
    if op.unit != "px":
        ctx.dpi = (float(op.dpi), float(op.dpi))
    return imops.resize(image, op.mode, width=op.width, height=op.height, scale=op.scale, size=op.size,
                        megapixels=op.megapixels, unit=op.unit, dpi=op.dpi, upscale=op.upscale,
                        resample=op.resample, background=op.background)


def _frame_pad(ctx: Ctx, op: Any, image: Image.Image) -> Image.Image:
    return imops.pad(image, top=op.top, right=op.right, bottom=op.bottom, left=op.left, color=op.color)


def _frame_adjust(ctx: Ctx, op: Any, image: Image.Image) -> Image.Image:
    alpha = image.getchannel("A") if image.mode == "RGBA" else None
    out = imops.adjust(image.convert("RGB") if alpha is not None else image, brightness=op.brightness,
                       contrast=op.contrast, saturation=op.saturation, sharpness=op.sharpness,
                       grayscale=op.grayscale, auto_contrast=op.auto_contrast)
    if alpha is not None:
        out = out.convert("RGBA")
        out.putalpha(alpha)
    return out


_FRAME_HANDLERS: dict[str, Callable[[Ctx, Any, Image.Image], Image.Image]] = {
    "rotate": _frame_rotate, "flip": _frame_flip, "crop": _frame_crop, "resize": _frame_resize,
    "pad": _frame_pad, "adjust": _frame_adjust,
}
_fallbacks_done = False


def _legacy_apply(name: str) -> Callable[[Ctx, Any, Sequence[BaseModel]], None]:
    def apply(ctx: Ctx, op: Any, prefix: Sequence[BaseModel]) -> None:
        _HANDLERS[name](ctx, op, prefix)  # looked up per call so tests can patch the module

    apply.__qualname__ = f"legacy_{name}"
    return apply


def _ensure_fallbacks() -> None:
    """Register the v1 handlers for every v1 op that no ``ops_ext`` plug-in has claimed (the shim)."""
    global _fallbacks_done
    if _fallbacks_done:
        return
    for model in schemas.V1_OPS:
        name = opreg._literal_name(model)
        assert name is not None
        opreg.register_op(OpSpec(name, model, _legacy_apply(name), frame_apply=_FRAME_HANDLERS.get(name),
                                 geometry=name in _V1_GEOMETRY, cost=OP_COST[name],
                                 fixed_cost=OP_FIXED_COST.get(name, 0.0), extra_bytes=OP_EXTRA_BYTES.get(name, 0),
                                 max_pixels=OP_MAX_PIXELS.get(name), label=OP_LABELS.get(name, "")),
                          fallback=True)
    _fallbacks_done = True


def _spec(name: str) -> OpSpec:
    try:
        return opreg.get_op(name)
    except KeyError:
        raise ValueError(f"unknown op {name!r}") from None


# ------------------------------------------------------------------ render
def has_alpha(img: Image.Image) -> bool:
    """True when the image has an alpha channel that is not fully opaque."""
    if img.mode in ("RGBA", "LA", "PA"):
        return img.getchannel("A").getextrema()[0] < 255
    return img.mode == "P" and "transparency" in img.info


def resolve_format(requested: str, source_format: str, img: Image.Image) -> str:
    """Concrete output format; ``original`` keeps the source format when it is writable."""
    if requested != "original":
        return requested
    fmt = source_format if source_format in OUTPUT_FORMATS and source_format != "pdf" else "jpeg"
    if fmt in _ALPHA_LESS and has_alpha(img):
        fmt = "png"
    return fmt


def render(ctx: Ctx, output: OutputSpec | dict | None = None) -> tuple[bytes, str, dict]:
    """Encode ``ctx.image``; returns ``(data, fmt, {"width","height","dpi","warnings","encode"})``.

    ``encode`` is ``{"quality","scale","met"}`` from the target-size encoder when max_kb/min_kb is set, else None.
    """
    if output is None:
        output = OutputSpec()
    elif isinstance(output, dict):
        output = OutputSpec.model_validate(output)
    img = ctx.image
    fmt = resolve_format(output.format, ctx.source_format, img)
    dpi = (float(output.dpi), float(output.dpi)) if output.dpi else ctx.dpi
    warnings = list(ctx.warnings)
    kwargs = dict(quality=output.quality, lossless=output.lossless, progressive=output.progressive,
                  chroma=output.chroma, dpi=dpi, exif=ctx.exif, icc_profile=ctx.icc_profile,
                  strip_metadata=output.strip_metadata, strip_gps=output.strip_gps,
                  colorspace=output.colorspace, background=output.background)
    width, height = img.size
    if ctx.animated:
        if fmt in ANIMATED_OUTPUTS and (fmt != "png" or ctx.source_format == "png"):
            if output.max_kb is not None or output.min_kb is not None:
                warnings.append("A target file size isn't supported for animations; the size was not adjusted")
            data = _encode_animation(ctx, fmt, output)
            return data, fmt, {"width": width, "height": height, "dpi": None, "warnings": warnings,
                               "encode": None, "frames": len(ctx.frames or ())}
        warnings.append(still_warning(fmt))
    cap = (LOSSLESS_ENCODE_MAX_PIXELS.get(fmt) if output.lossless else None) or ENCODE_MAX_PIXELS.get(fmt)
    if cap and width * height > cap:
        kind = f"{'Lossless ' if output.lossless else ''}{fmt.upper()}"
        raise PipelineError(413, f"{kind} output is limited to {cap / 1e6:.0f} MP (this image is "
                                 f"{width * height / 1e6:.0f} MP); resize it first or choose JPEG")
    encode = None
    if output.max_kb is not None or output.min_kb is not None:
        # output.quality is the ceiling; the encoder searches downwards (and downscales) from it.
        data, info = compress.encode_to_target(
            img, fmt, max_bytes=kb_to_bytes(output.max_kb), min_bytes=kb_to_bytes(output.min_kb), **kwargs,
        )
        if info.get("warning") and info["warning"] not in warnings:
            warnings.append(info["warning"])
        encode = {"quality": info.get("quality"), "scale": float(info.get("scale") or 1.0),
                  "met": bool(info.get("met"))}
        if info.get("width") and info.get("height"):
            width, height = int(info["width"]), int(info["height"])
        elif float(info.get("scale") or 1.0) != 1.0:
            scale = float(info["scale"])
            width, height = _encoded_size(data, round(width * scale), round(height * scale))
    else:
        data = io.save(img, fmt, **kwargs)
    return data, fmt, {
        "width": max(1, width),
        "height": max(1, height),
        "dpi": _dpi_json(_effective_dpi(fmt, dpi)),
        "warnings": warnings,
        "encode": encode,
    }


def kb_to_bytes(kb: float | None) -> int | None:
    """Whole bytes for a KB target, never 0 (0.0001 KB → 1 byte rather than an invalid 0)."""
    return None if kb is None else max(1, math.ceil(kb * 1024))


def _effective_dpi(fmt: str, dpi: tuple[float, float] | None) -> tuple[float, float] | None:
    """The resolution the encoded file actually carries (None for formats without one)."""
    if fmt not in DPI_FORMATS:
        return None
    if dpi is None and fmt == "pdf":
        return (float(PDF_DEFAULT_DPI), float(PDF_DEFAULT_DPI))
    return dpi


def _encoded_size(data: bytes, fallback_w: int, fallback_h: int) -> tuple[int, int]:
    try:
        with Image.open(BytesIO(data)) as im:
            return im.size
    except Exception:  # header unreadable without a plugin → use the computed size
        return fallback_w, fallback_h


def _dpi_json(dpi: tuple[float, float] | None) -> list[float] | None:
    if not dpi:
        return None
    return [int(v) if float(v).is_integer() else round(float(v), 2) for v in dpi]


_ensure_fallbacks()
