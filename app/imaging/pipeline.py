"""Run a list of ops over a loaded image and encode the result (PLAN.md §4).

``run`` applies ops in order to the EXIF-oriented image and tracks the pipeline DPI; ``render``
encodes with the output options; ``plan`` estimates a job's cost before any pixel is touched.
Errors: ValueError → 422, PipelineError carries its own status (413 too many pixels, 503 feature
unavailable), io.ImageError carries 413/415.
"""
from __future__ import annotations

import json
import math
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any

import numpy as np
from PIL import Image
from pydantic import BaseModel, ValidationError

from app.config import get_settings
from app.imaging import compress, io, ops as imops, presets
from app.schemas import (OPS_ADAPTER, OUTPUT_FORMATS, AdjustOp, BackgroundOp, CropAspectOp, CropOp, FlipOp,
                         OutputSpec, PadOp, PassportOp, ResizeOp, RotateOp, SheetOp, TrimOp, ratio_value)

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


class PipelineError(Exception):
    """A processing failure with an HTTP status (413 too large, 503 feature unavailable, …)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class FeatureUnavailable(PipelineError):
    def __init__(self, detail: str) -> None:
        super().__init__(503, detail)


@dataclass
class Ctx:
    image: Image.Image
    dpi: tuple[float, float] | None
    exif: bytes | None
    icc_profile: bytes | None
    source_format: str
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    cache_key: str = ""
    max_pixels: int = 0
    alive: Callable[[], bool] | None = None  # False once the upload is deleted (memo puts are undone)

    def warn(self, message: str | None) -> None:
        if message and message not in self.warnings:
            self.warnings.append(message)


def _nbytes(value: Any) -> int:
    """Array memory held by a cache value (a mask, or an object with a ``mask`` array)."""
    if isinstance(value, np.ndarray):
        return int(value.nbytes)
    mask = getattr(value, "mask", None)
    return int(mask.nbytes) if isinstance(mask, np.ndarray) else 0


class _LRU:
    """Tiny thread-safe LRU for expensive per-image analysis (faces, masks).

    Bounded by ``maxsize`` entries and ``max_bytes`` of array data; an entry larger than ``max_bytes``
    is not stored at all.
    """

    def __init__(self, maxsize: int, max_bytes: int | None = None) -> None:
        self.maxsize = maxsize
        self.max_bytes = max_bytes
        self._data: OrderedDict[str, Any] = OrderedDict()
        self._sizes: dict[str, int] = {}
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            value = self._data.get(key)
            if value is not None:
                self._data.move_to_end(key)
            return value

    def put(self, key: str, value: Any) -> None:
        size = _nbytes(value)
        with self._lock:
            self._pop(key)
            if self.max_bytes is not None and size > self.max_bytes:
                return
            self._data[key] = value
            self._sizes[key] = size
            self._bytes += size
            while len(self._data) > self.maxsize or self._over_budget():
                self._pop(next(iter(self._data)))

    def drop_prefix(self, prefix: str) -> None:
        with self._lock:
            for key in [k for k in self._data if k.startswith(prefix)]:
                self._pop(key)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._sizes.clear()
            self._bytes = 0

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._data)

    @property
    def nbytes(self) -> int:
        with self._lock:
            return self._bytes

    def _over_budget(self) -> bool:
        return self.max_bytes is not None and self._bytes > self.max_bytes

    def _pop(self, key: str) -> None:
        self._data.pop(key, None)
        self._bytes -= self._sizes.pop(key, 0)


_analysis_cache = _LRU(8, ANALYSIS_CACHE_BYTES)  # passport.analyze results
_mask_cache = _LRU(3, MASK_CACHE_BYTES)          # full-resolution person masks (float16) for the background op


def forget(cache_key: str) -> None:
    """Drop memoised analysis for an image (call when its upload is deleted)."""
    if cache_key:
        _analysis_cache.drop_prefix(cache_key + "|")
        _mask_cache.drop_prefix(cache_key + "|")


def clear_caches() -> None:
    _analysis_cache.clear()
    _mask_cache.clear()


def _memo_put(ctx: Ctx, cache: _LRU, key: str, value: Any) -> None:
    """Store, then undo if the upload was deleted meanwhile (its forget() may already have run)."""
    cache.put(key, value)
    if ctx.alive is not None and not ctx.alive():
        forget(ctx.cache_key)


# ------------------------------------------------------------------ run
def run(loaded: io.Loaded, ops: Sequence[BaseModel | dict], *, cache_key: str = "",
        max_pixels: int | None = None, alive: Callable[[], bool] | None = None) -> Ctx:
    """Apply ``ops`` in order.

    ``cache_key`` (the upload id) enables analysis memoisation; ``alive()`` reports whether that
    upload still exists, so a job finishing after a DELETE leaves nothing cached. ``max_pixels``
    defaults to HEYP_MAX_PIXELS.
    """
    op_list = _coerce_ops(ops)
    limit = max_pixels or get_settings().max_pixels
    ctx = Ctx(image=loaded.image, dpi=loaded.dpi, exif=loaded.exif, icc_profile=loaded.icc_profile,
              source_format=loaded.format, cache_key=cache_key, max_pixels=limit, alive=alive)
    if ctx.image.width * ctx.image.height > limit:
        raise PipelineError(413, f"Image is {ctx.image.width * ctx.image.height / 1e6:.0f} MP, "
                                 f"above the {limit / 1e6:.0f} MP limit")
    for index, op in enumerate(op_list):
        cap = OP_MAX_PIXELS.get(op.op)
        if cap and ctx.image.width * ctx.image.height > cap:
            raise PipelineError(413, f"{OP_LABELS[op.op]} works on images up to {cap / 1e6:.0f} MP (this one is "
                                     f"{ctx.image.width * ctx.image.height / 1e6:.0f} MP); crop or downsize it first")
        _HANDLERS[op.op](ctx, op, op_list[:index])
        _check_pixels(ctx.image.width, ctx.image.height, limit, op.op)
        if op.op != "passport" and op.op not in _KEEPS_GEOMETRY:
            ctx.meta.pop("passport_guides", None)  # its pixel positions no longer match the image
    return ctx


def passport_guides(ctx: Ctx) -> dict | None:
    """Passport measurements + preset bands in output px (PLAN v1.1 C2), or None.

    Only set when the last geometry-defining op was a passport op, so its coordinates match
    ``ctx.image`` (and the preview's full-resolution width/height).
    """
    return ctx.meta.get("passport_guides")


def _coerce_ops(ops: Sequence[BaseModel | dict]) -> list[BaseModel]:
    if all(isinstance(op, BaseModel) for op in ops):
        return list(ops)  # type: ignore[arg-type]
    try:
        raw = [op.model_dump() if isinstance(op, BaseModel) else op for op in ops]
        return list(OPS_ADAPTER.validate_python(raw))
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", ()))
        raise ValueError(f"invalid op ({loc}): {first.get('msg', 'invalid')}") from None


def _check_pixels(width: float, height: float, limit: int, what: str) -> None:
    pixels = width * height
    if pixels > limit:
        raise PipelineError(413, f"{what}: result would be {pixels / 1e6:.0f} MP, above the {limit / 1e6:.0f} MP limit")


def _memo_key(ctx: Ctx, kind: str, prefix: Sequence[BaseModel]) -> str | None:
    if not ctx.cache_key:
        return None
    chain = json.dumps([op.model_dump(mode="json") for op in prefix], sort_keys=True, separators=(",", ":"))
    return f"{ctx.cache_key}|{kind}|{chain}"


# ------------------------------------------------------------------ op handlers
def _rotate(ctx: Ctx, op: RotateOp, _prefix: Sequence[BaseModel]) -> None:
    if op.expand and op.angle % 90:
        rad = math.radians(op.angle)
        w, h = ctx.image.size
        bw = abs(w * math.cos(rad)) + abs(h * math.sin(rad))
        bh = abs(w * math.sin(rad)) + abs(h * math.cos(rad))
        _check_pixels(bw, bh, ctx.max_pixels, "rotate")
    ctx.image = imops.rotate(ctx.image, op.angle, expand=op.expand, fill=op.fill)


def _flip(ctx: Ctx, op: FlipOp, _prefix: Sequence[BaseModel]) -> None:
    ctx.image = imops.flip(ctx.image, op.direction)


def _crop(ctx: Ctx, op: CropOp, _prefix: Sequence[BaseModel]) -> None:
    ctx.image = imops.crop(ctx.image, op.x, op.y, op.width, op.height, unit=op.unit)


def _crop_aspect(ctx: Ctx, op: CropAspectOp, _prefix: Sequence[BaseModel]) -> None:
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


def _trim(ctx: Ctx, op: TrimOp, _prefix: Sequence[BaseModel]) -> None:
    ctx.image = imops.trim(ctx.image, tolerance=op.tolerance, padding=op.padding)


def _resize_estimate(size: tuple[int, int], op: ResizeOp) -> tuple[float, float]:
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


def _resize(ctx: Ctx, op: ResizeOp, _prefix: Sequence[BaseModel]) -> None:
    est_w, est_h = _resize_estimate(ctx.image.size, op)
    _check_pixels(est_w, est_h, ctx.max_pixels, "resize")
    ctx.image = imops.resize(
        ctx.image, op.mode, width=op.width, height=op.height, scale=op.scale, size=op.size,
        megapixels=op.megapixels, unit=op.unit, dpi=op.dpi, upscale=op.upscale, resample=op.resample,
        background=op.background,
    )
    if op.unit != "px":
        ctx.dpi = (float(op.dpi), float(op.dpi))


def _pad(ctx: Ctx, op: PadOp, _prefix: Sequence[BaseModel]) -> None:
    w, h = ctx.image.size
    _check_pixels(w + op.left + op.right, h + op.top + op.bottom, ctx.max_pixels, "pad")
    ctx.image = imops.pad(ctx.image, top=op.top, right=op.right, bottom=op.bottom, left=op.left, color=op.color)


def _adjust(ctx: Ctx, op: AdjustOp, _prefix: Sequence[BaseModel]) -> None:
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


def _background(ctx: Ctx, op: BackgroundOp, prefix: Sequence[BaseModel]) -> None:
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


def _passport_preset(op: PassportOp) -> presets.PassportPreset:
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


def _passport(ctx: Ctx, op: PassportOp, prefix: Sequence[BaseModel]) -> None:
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
    """The preview's ``passport`` object: measurements, guide lines (output px) and preset bands.

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
        "bands": {k: _float(bands.get(k)) for k in ("head_min", "head_max", "eye_min", "eye_max", "top_min", "top_max")},
        "background_edit_allowed": bool(info.get("background_edit_allowed",
                                                 getattr(preset, "background_edit_allowed", True))),
    }


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def sheet_dpi(op: SheetOp, pipeline_dpi: tuple[float, float] | None) -> int:
    """Explicit ``op.dpi``, else the pipeline dpi (e.g. a 400-dpi preset) clamped to 300–600, else 300."""
    if op.dpi:
        return op.dpi
    if pipeline_dpi and pipeline_dpi[0] > 0:
        lo, hi = SHEET_AUTO_DPI
        return int(min(hi, max(lo, round(pipeline_dpi[0]))))
    return SHEET_DEFAULT_DPI


def _sheet(ctx: Ctx, op: SheetOp, _prefix: Sequence[BaseModel]) -> None:
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
         source_format: str = "jpeg", dpi: float | None = None, decode: bool = True) -> Plan:
    """Estimate one file's job (decode → ops → encode) from its size alone, before running it.

    Sizes are upper bounds per op (crop keeps at most the input, passport/sheet use their fixed
    frames). Raises ValueError for ops that can't run at all (unknown preset or paper).
    """
    output = output or OutputSpec()
    w, h = (max(1.0, float(v)) for v in size)
    cur = src = w * h
    work = DECODE_COST * src / _MP if decode else 0.0
    step = 8 * src if decode else 0.0  # decoding briefly holds two copies
    peak_px = src
    for op in _coerce_ops(ops):
        (w, h), dpi = _planned_size((w, h), op, dpi)
        out = w * h
        work += OP_FIXED_COST.get(op.op, 0.0) + OP_COST[op.op] * max(cur, out) / _MP
        step = max(step, 4 * (src + cur + out) + OP_EXTRA_BYTES.get(op.op, 0) * cur)
        cur, peak_px = out, max(peak_px, out)
    fmt = output.format if output.format != "original" else (source_format or "jpeg")
    lossless = output.lossless and fmt in LOSSLESS_ENCODE_COST
    cost = LOSSLESS_ENCODE_COST[fmt] if lossless else ENCODE_COST.get(fmt, 0.1)
    if output.max_kb is not None or output.min_kb is not None:
        cost *= TARGET_SIZE_FACTOR
    work += cost * cur / _MP
    extra = LOSSLESS_EXTRA_BYTES[fmt] if lossless else ENCODE_EXTRA_BYTES.get(fmt, 4)
    step = max(step, 4 * src + (4 + extra) * cur)
    return Plan(work=round(work, 3), peak_bytes=int(step), peak_pixels=int(peak_px))


def _planned_size(size: tuple[float, float], op: BaseModel, dpi: float | None) -> tuple[tuple[float, float], float | None]:
    """Upper bound of an op's output size, and the pipeline dpi after it."""
    w, h = size
    if isinstance(op, RotateOp):
        if op.angle % 90 == 0:
            return ((h, w) if op.angle % 180 else (w, h)), dpi
        if not op.expand:
            return (w, h), dpi
        rad = math.radians(op.angle)
        c, s = abs(math.cos(rad)), abs(math.sin(rad))
        return (w * c + h * s, w * s + h * c), dpi
    if isinstance(op, CropOp):
        if op.unit == "fraction":
            return (max(1.0, w * op.width), max(1.0, h * op.height)), dpi
        return (max(1.0, min(w, op.width)), max(1.0, min(h, op.height))), dpi
    if isinstance(op, CropAspectOp):
        r = ratio_value(op.ratio)
        return ((h * r, h) if w / h > r else (w, w / r)), dpi
    if isinstance(op, TrimOp):
        return (w + 2 * op.padding, h + 2 * op.padding), dpi
    if isinstance(op, ResizeOp):
        out = _resize_estimate((w, h), op)
        return (max(1.0, out[0]), max(1.0, out[1])), (float(op.dpi) if op.unit != "px" else dpi)
    if isinstance(op, PadOp):
        return (w + op.left + op.right, h + op.top + op.bottom), dpi
    if isinstance(op, PassportOp):
        preset = _passport_preset(op)
        return tuple(float(v) for v in preset.output_size()), float(preset.dpi)  # type: ignore[return-value]
    if isinstance(op, SheetOp):
        paper = presets.PAPERS.get(op.paper)
        if paper is None:
            raise ValueError(f"unknown paper {op.paper!r} (choose from {', '.join(presets.PAPERS)})")
        d = sheet_dpi(op, (dpi, dpi)) if dpi else (op.dpi or SHEET_AUTO_DPI[1])  # unknown source dpi: upper bound
        return (paper.width_mm / 25.4 * d, paper.height_mm / 25.4 * d), float(d)
    return (w, h), dpi


_HANDLERS: dict[str, Callable[[Ctx, Any, Sequence[BaseModel]], None]] = {
    "rotate": _rotate, "flip": _flip, "crop": _crop, "crop_aspect": _crop_aspect, "trim": _trim,
    "resize": _resize, "pad": _pad, "adjust": _adjust, "background": _background,
    "passport": _passport, "sheet": _sheet,
}


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
