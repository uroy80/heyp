"""AI image ops (PLAN.md §6): ``upscale``, ``background`` (engines) and ``blur_regions``.

JSON (all fields but ``op`` optional unless noted):

* ``{"op": "upscale", "factor": 2|4 = 2, "model": "balanced"|"strong" = "balanced"}`` — Real-ESRGAN x4v3,
  tiled; refused (413) above HEYP_UPSCALE_MAX_INPUT_MP (default 6 MP) or when the result would exceed
  HEYP_MAX_PIXELS. Reports per-tile progress and honours cancellation inside jobs.
* ``{"op": "background", "mode": "remove"|"replace" (required), "color": "#ffffff",
  "engine": "auto"|"object"|"person" = "auto"}`` — v1 JSON still valid. ``object`` = BiRefNet_lite (any
  subject), ``person`` = U²-Net human segmentation (the passport model), ``auto`` = BiRefNet when installed,
  else the person model with a warning.
* ``{"op": "blur_regions", "regions": [{"x", "y", "w", "h", "shape": "rect"|"ellipse"}] (fractions),
  "mode": "blur"|"pixelate"|"solid" = "blur", "strength": 0…1 = 0.6, "color": "#000000"}`` — also per
  frame of animations. Suggested regions come from ``GET /api/detect/{file_id}`` (app/tools/img_ai.py).

Plug-in rules: imports only ``app.imaging.opreg`` and imaging helpers; models load lazily on first use.
"""
from __future__ import annotations

import dataclasses
from typing import Literal

import numpy as np
from pydantic import Field

from app.imaging import blur, matting, segment, upscale
from app.imaging.opreg import (Color, Ctx, FeatureUnavailable, OpModel, OpSpec, PipelineError, cache, memo_key,
                               memo_put, register_op)
from app.imaging.ortsession import ModelUnavailable

MASK_CACHE_BYTES = 256 * 1024 * 1024
BACKGROUND_MAX_PIXELS = 50_000_000  # compositing peaks at ~33 B/px (v1 measurement)
DEFAULT_MAX_PIXELS = 50_000_000     # HEYP_MAX_PIXELS default, used when a Ctx carries no limit


# ------------------------------------------------------------------ models
class UpscaleOp(OpModel):
    op: Literal["upscale"]
    factor: Literal[2, 4] = 2
    model: Literal["balanced", "strong"] = "balanced"


class BackgroundOp(OpModel):
    op: Literal["background"]
    mode: Literal["remove", "replace"]
    color: Color = "#ffffff"
    engine: Literal["auto", "object", "person"] = "auto"


class BlurRegion(OpModel):
    """A box in fractions of the image entering the op; parts outside the image are ignored."""

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(ge=0, le=1)
    h: float = Field(ge=0, le=1)
    shape: Literal["rect", "ellipse"] = "rect"


class BlurRegionsOp(OpModel):
    op: Literal["blur_regions"]
    regions: list[BlurRegion] = Field(default_factory=list, max_length=blur.MAX_REGIONS)
    mode: Literal["blur", "pixelate", "solid"] = "blur"
    strength: float = Field(default=0.6, ge=0, le=1)
    color: Color = "#000000"


# ------------------------------------------------------------------ upscale
def _upscale(ctx: Ctx, op: UpscaleOp, _prefix) -> None:
    if not upscale.available(op.model):
        raise FeatureUnavailable("Upscaling is unavailable: the upscale model is not installed on this server")
    try:
        upscale.check_limits(ctx.image.size, op.factor, ctx.max_pixels or DEFAULT_MAX_PIXELS)
    except upscale.LimitError as exc:
        raise PipelineError(413, str(exc)) from None
    message = f"Upscaling {op.factor}×"
    ctx.progress(0.0, message)
    try:
        ctx.image = upscale.upscale(ctx.image, op.factor, op.model,
                                    progress=lambda frac: ctx.progress(frac, message),
                                    check_cancel=ctx.check_cancel)
    except ModelUnavailable as exc:
        raise FeatureUnavailable(f"Upscaling is unavailable: {exc}") from None


def _upscale_size(size: tuple[float, float], op: UpscaleOp, dpi: float | None):
    return (size[0] * op.factor, size[1] * op.factor), dpi


# ------------------------------------------------------------------ background
def resolve_engine(requested: str, warn=None) -> str:
    """"object" or "person" for a requested engine; FeatureUnavailable (503) when nothing can run it."""
    if requested in ("auto", "object"):
        if matting.available():
            return "object"
        if requested == "object":
            raise FeatureUnavailable("Background removal (any subject) is unavailable: its model is not installed")
        if segment.available():
            if warn is not None:
                warn("The any-subject background model isn't installed here, so the people model was used")
            return "person"
        raise FeatureUnavailable("Background removal is unavailable: no segmentation model is installed")
    if requested == "person":
        if segment.available():
            return "person"
        raise FeatureUnavailable("Background removal (people) is unavailable: the person-segmentation model "
                                 "is not installed")
    raise ValueError(f"unknown background engine {requested!r}")


def _mask(ctx: Ctx, engine: str, prefix) -> np.ndarray:
    lru = cache("background-mask", 3, MASK_CACHE_BYTES)
    key = memo_key(ctx, f"mask-{engine}", prefix)
    mask = lru.get(key) if key else None
    if mask is not None and mask.shape == (ctx.image.height, ctx.image.width):
        return np.asarray(mask, dtype=np.float32)
    ctx.progress(0.05, "Finding the subject")
    try:
        if engine == "object":
            mask = matting.object_mask(ctx.image)
        else:
            mask = segment.person_mask(ctx.image)
    except RuntimeError as exc:  # model missing / failed to load (ModelUnavailable is one)
        raise FeatureUnavailable(f"Background removal is unavailable: {exc}") from None
    if key:
        memo_put(ctx, lru, key, mask.astype(np.float16))
    return mask


def _background(ctx: Ctx, op: BackgroundOp, prefix) -> None:
    engine = resolve_engine(op.engine, ctx.warn)
    mask = _mask(ctx, engine, prefix)
    ctx.progress(0.9, "Cutting out")
    color = "transparent" if op.mode == "remove" else op.color
    ctx.image = segment.composite(ctx.image, mask, color)


# ------------------------------------------------------------------ blur regions
def _blur_image(image, op: BlurRegionsOp, check_cancel=None):
    if not op.regions:
        return image
    return blur.blur_regions(image, op.regions, mode=op.mode, strength=op.strength,
                             color=segment.parse_color(op.color), check_cancel=check_cancel)


def _blur(ctx: Ctx, op: BlurRegionsOp, _prefix) -> None:
    ctx.image = _blur_image(ctx.image, op, ctx.check_cancel)


def _blur_frame(ctx: Ctx, op: BlurRegionsOp, frame):
    return _blur_image(frame, op, ctx.check_cancel)


# ------------------------------------------------------------------ registration
_SPEC_FIELDS = {f.name for f in dataclasses.fields(OpSpec)}


def _spec(**kwargs) -> OpSpec:
    """OpSpec from the keyword arguments this opreg version knows (newer hints are dropped quietly)."""
    return OpSpec(**{k: v for k, v in kwargs.items() if k in _SPEC_FIELDS})


SPECS = (
    # Upscale: ~42 core-seconds per input MP. `cost` is charged per MP of the (larger) output, which for 2×
    # is input×4 → 10.5 s/MP; 4× is over-estimated by 4× (safe side). Extra memory: the uint8 RGB result
    # array (3 B/output px = 48 B/input px at 4×) + the input/padded copies; the ORT session is ~0.2 GB.
    _spec(name="upscale", model=UpscaleOp, apply=_upscale, geometry=True, heavy=True, cost=10.5,
          fixed_cost=0.5, extra_bytes=54, fixed_bytes=250_000_000, label="Upscaling", plan_size=_upscale_size),
    # Background: BiRefNet costs ~11 core-seconds per call whatever the image size (it runs at 1024²) and
    # peaks at ~1.6 GB; compositing is ~0.04 s and ~29 B per pixel (v1 measurement).
    _spec(name="background", model=BackgroundOp, apply=_background, geometry=False, heavy=False, cost=0.04,
          fixed_cost=11.0, extra_bytes=29, fixed_bytes=matting.PEAK_BYTES, max_pixels=BACKGROUND_MAX_PIXELS,
          label="Background removal"),
    _spec(name="blur_regions", model=BlurRegionsOp, apply=_blur, frame_apply=_blur_frame, geometry=False,
          heavy=False, cost=0.05, extra_bytes=8, label="Blur"),
)

for _s in SPECS:
    # "background" existed in v1: replace=True wins over a v1 shim or a migrated copy.
    register_op(_s, replace=_s.name == "background")


__all__ = ["UpscaleOp", "BackgroundOp", "BlurRegion", "BlurRegionsOp", "resolve_engine", "SPECS"]
