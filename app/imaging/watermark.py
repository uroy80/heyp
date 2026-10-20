"""Text or image watermarks: 9-grid placement, a free position, or a tiled mosaic.

Sizes are fractions so a watermark planned on a preview lands identically on the full image (the
same rules as the UI's ``watermarkGeom`` in static/js/stagekit.js): text ``size`` is the font size
as a fraction of the image's SHORTER side, an image watermark's ``image_scale`` its width as a
fraction of the image width, ``margin`` a fraction of the shorter side. Text is set at weight 600.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from PIL import Image

from . import text

__all__ = ["POSITIONS", "MAX_TILES", "TEXT_WEIGHT", "apply", "build_layer", "place", "prepare", "stamp"]

POSITIONS = ("top-left", "top", "top-right", "left", "center", "right", "bottom-left", "bottom", "bottom-right")
MAX_TILES = 2500
TEXT_WEIGHT = 600
_GRID = {"top-left": ("left", "top"), "top": ("center", "top"), "top-right": ("right", "top"),
         "left": ("left", "center"), "center": ("center", "center"), "right": ("right", "center"),
         "bottom-left": ("left", "bottom"), "bottom": ("center", "bottom"), "bottom-right": ("right", "bottom")}
_ALIASES = {"top-center": "top", "center-left": "left", "middle": "center", "centre": "center",
            "center-right": "right", "bottom-center": "bottom", "middle-left": "left", "middle-right": "right"}


def _get(op: Any, name: str, default: Any = None) -> Any:
    if isinstance(op, dict):
        return op.get(name, default)
    return getattr(op, name, default)


def build_layer(op: Any, size: tuple[int, int], image: Image.Image | None = None) -> Image.Image:
    """The watermark as an RGBA layer (rotation and opacity applied) for a canvas of ``size``."""
    W, H = size
    opacity = float(_get(op, "opacity", 0.5))
    if not 0.0 <= opacity <= 1.0:
        raise ValueError("opacity must be between 0 and 1")
    kind = _get(op, "kind", "text")
    if kind == "text":
        content = str(_get(op, "text", "") or "")
        if not content.strip():
            raise ValueError("Enter the watermark text")
        size_px = float(_get(op, "size", 0.05)) * min(W, H)
        shadow = _get(op, "shadow")
        layer = text.render(content, font=_get(op, "font"), size=size_px, color=_get(op, "color", "#ffffff"),
                            weight=700 if _get(op, "bold", False) else TEXT_WEIGHT, align="center",
                            shadow=shadow if shadow else None,
                            shadow_offset=(max(1, round(size_px * 0.04)),) * 2 if shadow else None,
                            shadow_blur=size_px * 0.05 if shadow else 0.0)
    elif kind == "image":
        if image is None:
            raise ValueError("Choose an image for the watermark")
        wm = image.convert("RGBA")
        target_w = max(1, int(round(float(_get(op, "image_scale", 0.25)) * W)))
        target_h = max(1, int(round(wm.height * target_w / wm.width)))
        if target_w * target_h > 4 * W * H + 1_000_000:
            raise ValueError("The watermark image would be far larger than the photo; lower its scale")
        layer = wm.resize((target_w, target_h), Image.Resampling.LANCZOS, reducing_gap=3.0)
    else:
        raise ValueError(f"Invalid watermark kind {kind!r} (use 'text' or 'image')")
    rotation = float(_get(op, "rotation", 0.0) or 0.0)
    if rotation:
        layer = text.rotate_rgba(layer, rotation)
    if opacity < 1.0:
        layer.putalpha(layer.getchannel("A").point(lambda v: int(round(v * opacity))))
    return layer


def place(position: str, canvas: tuple[int, int], layer: tuple[int, int], margin: float,
          x: float | None = None, y: float | None = None) -> tuple[int, int]:
    """Top-left corner of the layer for a 9-grid ``position`` (or ``custom`` centre x/y fractions)."""
    W, H = canvas
    lw, lh = layer
    if position == "custom":
        cx = (0.5 if x is None else float(x)) * W
        cy = (0.5 if y is None else float(y)) * H
        return int(round(cx - lw / 2)), int(round(cy - lh / 2))
    position = _ALIASES.get(position, position)
    if position not in _GRID:
        raise ValueError(f"Invalid position {position!r} (use {', '.join(POSITIONS)}, mosaic or custom)")
    m = int(round(margin * min(W, H)))
    horizontal, vertical = _GRID[position]
    left = {"left": m, "center": (W - lw) // 2, "right": W - lw - m}[horizontal]
    top = {"top": m, "center": (H - lh) // 2, "bottom": H - lh - m}[vertical]
    return left, top


def _tile(base: Image.Image, layer: Image.Image, spacing: float) -> None:
    W, H = base.size
    lw, lh = layer.size
    gap_x = max(1, int(round(lw * spacing))) if spacing > 0 else 0
    gap_y = max(1, int(round(lh * spacing))) if spacing > 0 else 0
    step_x, step_y = max(1, lw + gap_x), max(1, lh + gap_y)
    cols, rows = math.ceil(W / step_x) + 2, math.ceil(H / step_y) + 2
    if cols * rows > MAX_TILES:
        raise ValueError("The mosaic would need too many copies; make the watermark bigger or the spacing wider")
    for row in range(-1, rows - 1):
        shift = (step_x // 2) if row % 2 else 0   # brick pattern reads better than a grid
        for col in range(-1, cols - 1):
            left, top = col * step_x + shift, row * step_y
            x0, y0 = max(0, left), max(0, top)
            x1, y1 = min(W, left + lw), min(H, top + lh)
            if x1 > x0 and y1 > y0:
                base.alpha_composite(layer.crop((x0 - left, y0 - top, x1 - left, y1 - top)), (x0, y0))


def prepare(op: Any, size: tuple[int, int], load_image: Callable[[str], Image.Image] | None = None) -> Image.Image:
    """The watermark layer for a canvas of ``size`` (rendered once; animations stamp it on every frame).
    ``load_image(file_id)`` resolves ``image_id`` for image watermarks."""
    watermark_image = None
    if _get(op, "kind", "text") == "image":
        image_id = _get(op, "image_id")
        if not image_id:
            raise ValueError("Choose an image for the watermark")
        if load_image is None:
            raise ValueError("Image watermarks are not available here")
        watermark_image = load_image(str(image_id))
    return build_layer(op, size, watermark_image)


def stamp(img: Image.Image, op: Any, layer: Image.Image) -> Image.Image:
    """Composite a prepared watermark ``layer`` onto ``img`` at the op's position (or tiled)."""
    had_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    base = img.convert("RGBA")
    position = _get(op, "position", "bottom-right") or "bottom-right"
    if position == "mosaic":
        spacing = _get(op, "spacing", 0.5)
        _tile(base, layer, 0.5 if spacing is None else float(spacing))
    else:
        left, top = place(position, base.size, layer.size, float(_get(op, "margin", 0.03) or 0.0),
                          _get(op, "x"), _get(op, "y"))
        W, H = base.size
        x0, y0 = max(0, left), max(0, top)
        x1, y1 = min(W, left + layer.width), min(H, top + layer.height)
        if x1 > x0 and y1 > y0:
            base.alpha_composite(layer.crop((x0 - left, y0 - top, x1 - left, y1 - top)), (x0, y0))
    return base if had_alpha else base.convert("RGB")


def apply(img: Image.Image, op: Any, load_image: Callable[[str], Image.Image] | None = None) -> Image.Image:
    """Watermark ``img`` per ``op`` (a WatermarkOp or dict). ``load_image(file_id)`` resolves
    ``image_id`` for image watermarks."""
    return stamp(img, op, prepare(op, img.size, load_image))
