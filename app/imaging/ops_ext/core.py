"""The v1 geometry and tone ops as registry plug-ins (PLAN.md §3 "Ops").

``rotate``, ``flip``, ``crop``, ``crop_aspect``, ``trim``, ``resize``, ``pad`` and ``adjust`` keep their v1
JSON exactly (the models are the v1 ones from ``app.schemas``); ``rotate`` gains one optional field:

* ``{"op": "rotate", "angle": 90, "only": "all"|"landscape"|"portrait"}`` — batch filter: with
  ``landscape`` only images wider than tall are rotated (``portrait``: taller than wide); the others
  pass through unchanged. Default ``all`` = v1 behaviour.

Every op also runs per frame of an animation. Content-dependent geometry is decided once per
animation so all frames keep one size: ``trim`` crops the union of every frame's content box and
``crop_aspect`` (smart/face) places its window from the first frame.

``background`` (ops_ext/ai.py) and ``passport``/``sheet`` (passport-specific, still served by
the pipeline's v1 handlers) are not registered here.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any, Literal

from PIL import Image
from pydantic import BaseModel

from app.imaging import ops as imops
from app.imaging.opreg import Ctx, OpSpec, check_pixels, register_op
from app.schemas import (AdjustOp, CropAspectOp, CropOp, FlipOp, PadOp, ResizeOp, RotateOp as V1RotateOp,
                         TrimOp)

_UNIT_PER_INCH = {"in": 1.0, "cm": 2.54, "mm": 25.4}
ADJUST_MAX_PIXELS = 100_000_000  # adjust peaks at ≈ 11 B/px (v1 measurement)


class RotateOp(V1RotateOp):
    """v1 rotate plus the batch filter ``only``."""

    only: Literal["all", "landscape", "portrait"] = "all"


def _matches(size: tuple[int, int], only: str) -> bool:
    w, h = size
    if only == "landscape":
        return w > h
    if only == "portrait":
        return h > w
    return True


def _meta_key(op: BaseModel, kind: str) -> str:
    """Per-op scratch key in ``ctx.meta`` (one op object = one entry of the request's op list)."""
    return f"_{kind}:{id(op)}"


# ------------------------------------------------------------------ rotate / flip
def _rotate_check(ctx: Ctx, size: tuple[int, int], op: RotateOp) -> None:
    if op.expand and op.angle % 90:
        rad = math.radians(op.angle)
        w, h = size
        check_pixels(abs(w * math.cos(rad)) + abs(h * math.sin(rad)),
                     abs(w * math.sin(rad)) + abs(h * math.cos(rad)), ctx.max_pixels or imops_limit(), "rotate")


def imops_limit() -> int:
    from app.imaging import io

    return io.max_pixels()


def _rotate(ctx: Ctx, op: RotateOp, _prefix: Sequence[BaseModel]) -> None:
    if not _matches(ctx.image.size, op.only):
        return
    _rotate_check(ctx, ctx.image.size, op)
    ctx.image = imops.rotate(ctx.image, op.angle, expand=op.expand, fill=op.fill)


def _rotate_frame(ctx: Ctx, op: RotateOp, frame: Image.Image) -> Image.Image:
    if not _matches(frame.size, op.only):
        return frame
    if ctx.frame_index == 0:
        _rotate_check(ctx, frame.size, op)
    return imops.rotate(frame, op.angle, expand=op.expand, fill=op.fill)


def _rotate_plan(size: tuple[float, float], op: RotateOp, dpi: float | None) -> tuple[tuple[float, float], float | None]:
    w, h = size
    if not _matches((int(w), int(h)), op.only):
        return (w, h), dpi
    if op.angle % 90 == 0:
        return ((h, w) if op.angle % 180 else (w, h)), dpi
    if not op.expand:
        return (w, h), dpi
    rad = math.radians(op.angle)
    c, s = abs(math.cos(rad)), abs(math.sin(rad))
    return (w * c + h * s, w * s + h * c), dpi


def _flip(ctx: Ctx, op: FlipOp, _prefix: Sequence[BaseModel]) -> None:
    ctx.image = imops.flip(ctx.image, op.direction)


def _flip_frame(ctx: Ctx, op: FlipOp, frame: Image.Image) -> Image.Image:
    return imops.flip(frame, op.direction)


# ------------------------------------------------------------------ crops
def _crop(ctx: Ctx, op: CropOp, _prefix: Sequence[BaseModel]) -> None:
    ctx.image = imops.crop(ctx.image, op.x, op.y, op.width, op.height, unit=op.unit)


def _crop_frame(ctx: Ctx, op: CropOp, frame: Image.Image) -> Image.Image:
    return imops.crop(frame, op.x, op.y, op.width, op.height, unit=op.unit)


def _detect_faces(img: Image.Image) -> list:
    try:
        from app.imaging import face
    except ImportError:
        return []
    try:
        if hasattr(face, "available") and not face.available():
            return []
        return face.detect(img)
    except Exception:  # no detector → smart crop (with a warning) instead of failing the edit
        return []


def _aspect_box(ctx: Ctx, op: CropAspectOp, img: Image.Image) -> tuple[int, int, int, int]:
    """The window crop_aspect cuts from ``img`` (decided once per animation, reused for every frame)."""
    faces = None
    if op.anchor == "face":
        faces = _detect_faces(img)
        if not faces:
            ctx.warn("No face found — used smart crop instead")
    return imops.crop_aspect_box(img, op.ratio, anchor=op.anchor, faces=faces)


def _crop_aspect(ctx: Ctx, op: CropAspectOp, _prefix: Sequence[BaseModel]) -> None:
    faces = None
    if op.anchor == "face":
        faces = _detect_faces(ctx.image)
        if not faces:
            ctx.warn("No face found — used smart crop instead")
    ctx.image = imops.crop_aspect(ctx.image, op.ratio, anchor=op.anchor, faces=faces)


def _crop_aspect_frame(ctx: Ctx, op: CropAspectOp, frame: Image.Image) -> Image.Image:
    key = _meta_key(op, "crop_aspect")
    box = ctx.meta.get(key)
    if box is None:
        first = ctx.frames[0] if ctx.frames else frame
        box = ctx.meta[key] = _aspect_box(ctx, op, first)
    return frame.crop(box)


def _trim(ctx: Ctx, op: TrimOp, _prefix: Sequence[BaseModel]) -> None:
    ctx.image = imops.trim(ctx.image, tolerance=op.tolerance, padding=op.padding)


def _trim_frame(ctx: Ctx, op: TrimOp, frame: Image.Image) -> Image.Image:
    key = _meta_key(op, "trim")
    box = ctx.meta.get(key)
    if box is None:
        frames = ctx.frames or [frame]
        left, top, right, bottom = frames[0].width, frames[0].height, 0, 0
        for f in frames:  # union of every frame's content box: no frame loses content
            ctx.check_cancel()
            found = imops.trim_box(f, op.tolerance)
            if found is not None:
                x0, y0, x1, y1 = found
                left, top, right, bottom = min(left, x0), min(top, y0), max(right, x1), max(bottom, y1)
        if right <= left or bottom <= top:
            left, top, right, bottom = 0, 0, frames[0].width, frames[0].height
        W, H = frames[0].size
        p = op.padding
        box = ctx.meta[key] = (max(0, left - p), max(0, top - p), min(W, right + p), min(H, bottom + p))
    return frame.crop(box)


# ------------------------------------------------------------------ resize / pad / adjust
def _resize_estimate(size: tuple[float, float], op: ResizeOp) -> tuple[float, float]:
    """Upper bound of the resize result (refuses absurd sizes before allocating them)."""
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


def _do_resize(ctx: Ctx, op: ResizeOp, img: Image.Image) -> Image.Image:
    est_w, est_h = _resize_estimate(img.size, op)
    check_pixels(est_w, est_h, ctx.max_pixels or imops_limit(), "resize")
    out = imops.resize(img, op.mode, width=op.width, height=op.height, scale=op.scale, size=op.size,
                       megapixels=op.megapixels, unit=op.unit, dpi=op.dpi, upscale=op.upscale,
                       resample=op.resample, background=op.background)
    if op.unit != "px":
        ctx.dpi = (float(op.dpi), float(op.dpi))
    return out


def _resize(ctx: Ctx, op: ResizeOp, _prefix: Sequence[BaseModel]) -> None:
    ctx.image = _do_resize(ctx, op, ctx.image)


def _resize_frame(ctx: Ctx, op: ResizeOp, frame: Image.Image) -> Image.Image:
    return _do_resize(ctx, op, frame)


def _pad(ctx: Ctx, op: PadOp, _prefix: Sequence[BaseModel]) -> None:
    w, h = ctx.image.size
    check_pixels(w + op.left + op.right, h + op.top + op.bottom, ctx.max_pixels or imops_limit(), "pad")
    ctx.image = imops.pad(ctx.image, top=op.top, right=op.right, bottom=op.bottom, left=op.left, color=op.color)


def _pad_frame(ctx: Ctx, op: PadOp, frame: Image.Image) -> Image.Image:
    return imops.pad(frame, top=op.top, right=op.right, bottom=op.bottom, left=op.left, color=op.color)


def _adjust_image(op: AdjustOp, img: Image.Image) -> Image.Image:
    return imops.adjust(img, brightness=op.brightness, contrast=op.contrast, saturation=op.saturation,
                        sharpness=op.sharpness, grayscale=op.grayscale, auto_contrast=op.auto_contrast)


def _adjust(ctx: Ctx, op: AdjustOp, _prefix: Sequence[BaseModel]) -> None:
    ctx.image = _adjust_image(op, ctx.image)


def _adjust_frame(ctx: Ctx, op: AdjustOp, frame: Image.Image) -> Image.Image:
    out = _adjust_image(op, frame)
    # Grayscale frames stay in the animation's colour mode so every frame encodes alike.
    if frame.mode == "RGBA" and out.mode == "LA":
        return out.convert("RGBA")
    if frame.mode == "RGB" and out.mode == "L":
        return out.convert("RGB")
    return out


# ------------------------------------------------------------------ registration
def _spec(name: str, model: type[BaseModel], apply: Any, frame: Any, **kw: Any) -> OpSpec:
    return register_op(OpSpec(name, model, apply, frame_apply=frame, **kw))


ROTATE = _spec("rotate", RotateOp, _rotate, _rotate_frame, geometry=True, cost=0.04, extra_bytes=3,
               plan_size=_rotate_plan, label="Rotate")
FLIP = _spec("flip", FlipOp, _flip, _flip_frame, geometry=True, cost=0.005, extra_bytes=0, label="Flip")
CROP = _spec("crop", CropOp, _crop, _crop_frame, geometry=True, cost=0.002, extra_bytes=0, label="Crop")
CROP_ASPECT = _spec("crop_aspect", CropAspectOp, _crop_aspect, _crop_aspect_frame, geometry=True, cost=0.01,
                    fixed_cost=0.2, extra_bytes=2, label="Crop to ratio")
TRIM = _spec("trim", TrimOp, _trim, _trim_frame, geometry=True, cost=0.01, extra_bytes=2, label="Trim")
RESIZE = _spec("resize", ResizeOp, _resize, _resize_frame, geometry=True, cost=0.015, extra_bytes=3,
               label="Resize")
PAD = _spec("pad", PadOp, _pad, _pad_frame, geometry=True, cost=0.005, extra_bytes=0, label="Pad")
ADJUST = _spec("adjust", AdjustOp, _adjust, _adjust_frame, cost=0.02, extra_bytes=7,
               max_pixels=ADJUST_MAX_PIXELS, label="Adjustments")
