"""Photo editor engine: named filters, frames and an overlay layer (text, stickers, shapes, brush).

Everything is deterministic per frame (animations apply the same edit to every frame) and works in
fractions of the current image, so an edit planned on a preview applies unchanged to the full-size
image. Filters run on row strips (bounded float memory); overlay items render into layers no larger
than their own bounding box.

Public API: :data:`FILTERS`, :data:`FRAMES`, :func:`apply_filter`, :func:`apply_frame`,
:func:`apply_overlay`, :func:`stickers`, :func:`sticker_svg`, :func:`render_sticker`, :func:`catalog`.
"""
from __future__ import annotations

import functools
import json
import math
import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

from . import ops, text
from .ops import parse_color

__all__ = [
    "FILTERS", "FRAMES", "SHAPES", "apply_filter", "apply_frame", "frame_size", "apply_overlay", "prepare_overlay",
    "composite_overlay", "Placed", "stickers", "sticker_svg", "sticker_ratio", "has_sticker",
    "render_sticker", "catalog", "STICKER_DIR", "MAX_ITEMS", "MAX_BRUSH_POINTS",
]

STICKER_DIR = Path(__file__).resolve().parent.parent / "assets" / "stickers"
STRIP_PIXELS = 2_000_000          # float32 working set per filter strip ≈ 24 MB
MAX_ITEMS = 100
MAX_BRUSH_POINTS = 20_000         # across all brush strokes of one overlay
MAX_LAYER_PIXELS = 120_000_000    # one item's layer (before rotation)
MAX_STICKER_PX = 4096
SHAPES = ("rect", "ellipse", "line", "arrow", "star", "heart")
_INK = (0x1D, 0x1B, 0x19)          # Broadsheet ink / paper (static/styles.css tokens)
_PAPER = (0xF2, 0xEC, 0xDF)


# ------------------------------------------------------------------ colour helpers (float RGB in 0..1)
def _lum(a: np.ndarray) -> np.ndarray:
    return a[..., 0] * 0.2126 + a[..., 1] * 0.7152 + a[..., 2] * 0.0722


def _sat(a: np.ndarray, s: float) -> np.ndarray:
    lum = _lum(a)[..., None]
    return lum + (a - lum) * s


def _contrast(a: np.ndarray, c: float, pivot: float = 0.5) -> np.ndarray:
    return (a - pivot) * c + pivot


def _scurve(a: np.ndarray, k: float) -> np.ndarray:
    """Smooth S-curve (k > 0 adds contrast around mid grey, keeps 0 and 1 fixed)."""
    x = np.clip(a, 0.0, 1.0)
    s = x * x * (3 - 2 * x)
    return x + (s - x) * k


def _lift(a: np.ndarray, black: float, white: float = 1.0) -> np.ndarray:
    return black + a * (white - black)


def _mul(a: np.ndarray, rgb: Sequence[float], add: Sequence[float] = (0.0, 0.0, 0.0)) -> np.ndarray:
    return a * np.asarray(rgb, np.float32) + np.asarray(add, np.float32)


def _duotone(a: np.ndarray, dark: Sequence[int], light: Sequence[int], contrast: float = 1.0) -> np.ndarray:
    lum = np.clip(_contrast(_lum(a), contrast), 0.0, 1.0)[..., None]
    d, l_ = np.asarray(dark, np.float32) / 255, np.asarray(light, np.float32) / 255
    return d + (l_ - d) * lum


def _split_tone(a: np.ndarray, shadows: Sequence[float], highlights: Sequence[float], amount: float) -> np.ndarray:
    lum = _lum(a)[..., None]
    sh, hi = np.asarray(shadows, np.float32), np.asarray(highlights, np.float32)
    tint = sh * (1 - lum) + hi * lum
    return a + (tint - 0.5) * amount


def _sepia(a: np.ndarray) -> np.ndarray:
    m = np.array([[0.393, 0.769, 0.189], [0.349, 0.686, 0.168], [0.272, 0.534, 0.131]], np.float32)
    return a @ m.T


@dataclass
class _Strip:
    y0: int
    width: int
    height: int
    aux: np.ndarray | None = None   # per-filter auxiliary rows (e.g. a blurred copy), 0..1 floats


def _vignette(strip: _Strip, rows: int, strength: float) -> np.ndarray:
    """Multiplicative radial falloff for the strip's rows (1 at the centre, 1-strength at the corners)."""
    ys = (np.arange(strip.y0, strip.y0 + rows, dtype=np.float32) + 0.5) / strip.height - 0.5
    xs = (np.arange(strip.width, dtype=np.float32) + 0.5) / strip.width - 0.5
    d = np.sqrt(ys[:, None] ** 2 + xs[None, :] ** 2) / math.sqrt(0.5)
    return (1.0 - strength * np.clip((d - 0.35) / 0.65, 0.0, 1.0) ** 1.6)[..., None]


def _halftone(a: np.ndarray, strip: _Strip) -> np.ndarray:
    """Newsprint dot screen at 45°: ink dots whose area follows the darkness."""
    rows = a.shape[0]
    cell = max(4.0, min(strip.width, strip.height) / 140.0)
    ys = np.arange(strip.y0, strip.y0 + rows, dtype=np.float32)[:, None] + 0.5
    xs = np.arange(strip.width, dtype=np.float32)[None, :] + 0.5
    c = s = math.sqrt(0.5)
    u, v = (xs * c + ys * s) / cell, (-xs * s + ys * c) / cell
    du, dv = u - np.floor(u) - 0.5, v - np.floor(v) - 0.5
    dist = np.sqrt(du * du + dv * dv)                      # 0 at the cell centre, ≈0.707 at its corner
    darkness = 1.0 - np.clip(_lum(a), 0.0, 1.0)
    radius = np.sqrt(darkness / math.pi)                    # dot area ∝ darkness (cell area 1)
    ink = np.clip((radius - dist) * cell + 0.5, 0.0, 1.0)[..., None]   # 1 px anti-aliasing
    paper, inkc = np.asarray(_PAPER, np.float32) / 255, np.asarray(_INK, np.float32) / 255
    return paper + (inkc - paper) * ink


def _pencil(a: np.ndarray, strip: _Strip) -> np.ndarray:
    gray = np.clip(_lum(a), 0.0, 1.0)
    blurred_inv = strip.aux[..., 0] if strip.aux is not None else 1.0 - gray
    # Colour dodge of the gray image with its blurred negative: flat areas go white, edges stay dark.
    dodge = np.clip(gray / np.maximum(1e-3, 1.0 - blurred_inv), 0.0, 1.0)
    out = dodge ** 1.8
    return np.repeat(out[..., None], 3, axis=2)


def _dramatic(a: np.ndarray, strip: _Strip) -> np.ndarray:
    base = a if strip.aux is None else a + (a - strip.aux) * 0.9     # large-radius local contrast
    return _sat(_contrast(base, 1.08), 0.8)


def _matrix(a: np.ndarray, m: Sequence[Sequence[float]]) -> np.ndarray:
    return np.clip(a @ np.asarray(m, np.float32).T, 0.0, 1.0)


def _css(a: np.ndarray, chain: Sequence[tuple[str, float]]) -> np.ndarray:
    """A CSS ``filter`` chain (W3C Filter Effects matrices, sRGB, clamped after every step) — the same
    maths the browser uses for the UI's swatches, so those previews match the server's result."""
    for name, v in chain:
        if name == "grayscale":
            s = 1.0 - v
            a = _matrix(a, ((0.2126 + 0.7874 * s, 0.7152 - 0.7152 * s, 0.0722 - 0.0722 * s),
                            (0.2126 - 0.2126 * s, 0.7152 + 0.2848 * s, 0.0722 - 0.0722 * s),
                            (0.2126 - 0.2126 * s, 0.7152 - 0.7152 * s, 0.0722 + 0.9278 * s)))
        elif name == "sepia":
            s = 1.0 - v
            a = _matrix(a, ((0.393 + 0.607 * s, 0.769 - 0.769 * s, 0.189 - 0.189 * s),
                            (0.349 - 0.349 * s, 0.686 + 0.314 * s, 0.168 - 0.168 * s),
                            (0.272 - 0.272 * s, 0.534 - 0.534 * s, 0.131 + 0.869 * s)))
        elif name == "saturate":
            a = _matrix(a, ((0.213 + 0.787 * v, 0.715 - 0.715 * v, 0.072 - 0.072 * v),
                            (0.213 - 0.213 * v, 0.715 + 0.285 * v, 0.072 - 0.072 * v),
                            (0.213 - 0.213 * v, 0.715 - 0.715 * v, 0.072 + 0.928 * v)))
        elif name == "hue-rotate":
            c, s = math.cos(math.radians(v)), math.sin(math.radians(v))
            a = _matrix(a, ((0.213 + c * 0.787 - s * 0.213, 0.715 - c * 0.715 - s * 0.715, 0.072 - c * 0.072 + s * 0.928),
                            (0.213 - c * 0.213 + s * 0.143, 0.715 + c * 0.285 + s * 0.140, 0.072 - c * 0.072 - s * 0.283),
                            (0.213 - c * 0.213 - s * 0.787, 0.715 - c * 0.715 + s * 0.715, 0.072 + c * 0.928 + s * 0.072)))
        elif name == "brightness":
            a = np.clip(a * v, 0.0, 1.0)
        elif name == "contrast":
            a = np.clip((a - 0.5) * v + 0.5, 0.0, 1.0)
        else:  # pragma: no cover — chains are ours
            raise ValueError(name)
    return a


def _unsharp(a: np.ndarray, strip: _Strip, amount: float) -> np.ndarray:
    return a if strip.aux is None else a + (a - strip.aux) * amount


def _soft(a: np.ndarray, strip: _Strip) -> np.ndarray:
    blurred = a if strip.aux is None else strip.aux
    glow = np.maximum(a, blurred)  # highlights bloom, shadows stay put
    return _css(a * 0.45 + blurred * 0.3 + glow * 0.25, (("brightness", 1.05),))


_FilterFn = Callable[[np.ndarray, _Strip], np.ndarray]


@dataclass(frozen=True)
class FilterSpec:
    id: str
    name: str
    fn: _FilterFn
    aux: str | None = None          # "blur_inverted_gray" | "blur" — computed once at full size
    aux_radius: float = 0.0         # fraction of the shorter side


def _f(fn: Callable[[np.ndarray], np.ndarray]) -> _FilterFn:
    return lambda a, _strip: fn(a)


FILTERS: dict[str, FilterSpec] = {spec.id: spec for spec in (
    FilterSpec("vivid", "Vivid", _f(lambda a: _contrast(_sat(a, 1.35), 1.08))),
    FilterSpec("warm", "Warm", _f(lambda a: _sat(_mul(a, (1.06, 1.01, 0.88), (0.02, 0.005, 0.0)), 1.05))),
    FilterSpec("cool", "Cool", _f(lambda a: _mul(a, (0.92, 0.99, 1.07), (0.0, 0.005, 0.025)))),
    FilterSpec("golden", "Golden hour", _f(lambda a: _sat(_split_tone(_mul(a, (1.05, 1.0, 0.9)),
                                                                      (0.45, 0.42, 0.5), (1.0, 0.78, 0.45), 0.3),
                                                          1.1))),
    FilterSpec("fade", "Fade", _f(lambda a: _sat(_contrast(_lift(a, 0.09, 0.96), 0.92), 0.78))),
    FilterSpec("vintage", "Vintage", lambda a, s: (_lift(_contrast(a * 0.65 + _sepia(a) * 0.35, 0.9), 0.06, 0.97)
                                                    * _vignette(s, a.shape[0], 0.3))),
    FilterSpec("chrome", "Chrome", _f(lambda a: _split_tone(_sat(_contrast(a, 1.2), 1.15),
                                                            (0.38, 0.47, 0.6), (0.55, 0.52, 0.5), 0.18))),
    FilterSpec("dramatic", "Dramatic", _dramatic, aux="blur", aux_radius=0.03),
    FilterSpec("lomo", "Lomo", lambda a, s: _sat(_contrast(a, 1.22), 1.3) * _vignette(s, a.shape[0], 0.6)),
    FilterSpec("teal_orange", "Teal & orange", _f(lambda a: _contrast(_split_tone(a, (0.15, 0.5, 0.55),
                                                                                 (1.0, 0.62, 0.32), 0.32), 1.05))),
    FilterSpec("mono", "Mono", _f(lambda a: np.repeat(_lum(a)[..., None], 3, axis=2))),
    FilterSpec("noir", "Noir", lambda a, s: np.repeat(_scurve(_contrast(_lum(a), 1.25), 0.8)[..., None], 3, axis=2)
               * _vignette(s, a.shape[0], 0.25)),
    FilterSpec("sepia", "Sepia", _f(_sepia)),
    FilterSpec("broadsheet", "Broadsheet", _f(lambda a: _duotone(a, _INK, _PAPER, 1.15))),
    FilterSpec("cyanotype", "Cyanotype", _f(lambda a: _duotone(a, (0x0B, 0x3A, 0x6E), (0xE9, 0xF2, 0xF9), 1.1))),
    FilterSpec("halftone", "Halftone", _halftone),
    FilterSpec("pencil", "Pencil", _pencil, aux="blur_inverted_gray", aux_radius=0.012),
    FilterSpec("invert", "Invert", _f(lambda a: 1.0 - a)),
    # Named after the looks the UI offers; CSS chains identical to the UI's swatches (static/js/catalog.js).
    FilterSpec("matte", "Matte", _f(lambda a: _css(a, (("contrast", 0.85), ("saturate", 0.9), ("brightness", 1.06)))
                                   * 0.94 + 0.04)),
    FilterSpec("polaroid", "Instant", _f(lambda a: _css(a, (("sepia", 0.18), ("contrast", 1.08), ("saturate", 1.2),
                                                           ("brightness", 1.05))))),
    FilterSpec("kodachrome", "Slide film", _f(lambda a: _css(a, (("sepia", 0.2), ("saturate", 1.45), ("contrast", 1.15),
                                                                ("hue-rotate", -5))))),
    FilterSpec("technicolor", "Cinema", _f(lambda a: _css(a, (("saturate", 1.75), ("contrast", 1.2),
                                                             ("hue-rotate", 4))))),
    FilterSpec("bright", "Bright", _f(lambda a: _css(a, (("brightness", 1.2), ("contrast", 1.05))))),
    FilterSpec("sharpen", "Sharpen", lambda a, s: _unsharp(a, s, 1.2), aux="blur", aux_radius=0.0015),
    FilterSpec("soft", "Soft focus", _soft, aux="blur", aux_radius=0.006),
)}
FILTER_ALIASES = {"grayscale": "mono", "greyscale": "mono", "black_white": "noir", "teal-orange": "teal_orange",
                  "golden-hour": "golden", "soft-focus": "soft"}


def _split_alpha(img: Image.Image) -> tuple[Image.Image, Image.Image | None]:
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        return rgba.convert("RGB"), rgba.getchannel("A")
    return (img if img.mode == "RGB" else img.convert("RGB")), None


def _aux_image(rgb: Image.Image, spec: FilterSpec) -> Image.Image | None:
    if spec.aux is None:
        return None
    radius = max(1.0, spec.aux_radius * min(rgb.size))
    if spec.aux == "blur":
        return rgb.filter(ImageFilter.GaussianBlur(radius))
    inverted = Image.eval(rgb.convert("L"), lambda v: 255 - v)
    return inverted.filter(ImageFilter.GaussianBlur(radius))


def apply_filter(img: Image.Image, name: str, intensity: float = 1.0) -> Image.Image:
    """Apply a named filter blended with the original by ``intensity`` (0 = unchanged, 1 = full)."""
    if name == "none":
        return img.copy()
    spec = FILTERS.get(FILTER_ALIASES.get(name, name))
    if spec is None:
        raise ValueError(f"Unknown filter {name!r} (choose from {', '.join(FILTERS)})")
    intensity = float(intensity)
    if not math.isfinite(intensity) or not 0.0 <= intensity <= 1.0:
        raise ValueError("intensity must be between 0 and 1")
    if intensity == 0.0:
        return img.copy()
    rgb, alpha = _split_alpha(img)
    width, height = rgb.size
    aux_img = _aux_image(rgb, spec)
    src = np.asarray(rgb)
    aux_arr = np.asarray(aux_img) if aux_img is not None else None
    out = np.empty_like(src)
    rows = max(1, STRIP_PIXELS // max(1, width))
    for y0 in range(0, height, rows):
        block = src[y0:y0 + rows].astype(np.float32) / 255.0
        aux = None
        if aux_arr is not None:
            aux = aux_arr[y0:y0 + rows].astype(np.float32) / 255.0
            if aux.ndim == 2:
                aux = aux[..., None]
        result = spec.fn(block, _Strip(y0, width, height, aux))
        if intensity < 1.0:
            result = block + (result - block) * intensity
        out[y0:y0 + rows] = np.clip(result * 255.0 + 0.5, 0, 255).astype(np.uint8)
    result_img = Image.fromarray(out, "RGB")
    if alpha is not None:
        result_img.putalpha(alpha)
    return result_img


# ------------------------------------------------------------------ frames
FRAMES: dict[str, dict[str, Any]] = {
    "line": {"name": "Line", "width": 0.03, "color": "#1d1b19"},
    "double": {"name": "Double rule", "width": 0.05, "color": "#1d1b19"},
    "polaroid": {"name": "Instant photo", "width": 0.05, "color": "#ffffff"},
    "mat": {"name": "Mat board", "width": 0.08, "color": "#f2ecdf"},
    "rounded": {"name": "Rounded corners", "width": 0.06, "color": "transparent"},
    "shadow": {"name": "Drop shadow", "width": 0.05, "color": "#00000073"},
    "film": {"name": "Film strip", "width": 0.08, "color": "#141414"},
    "stamp": {"name": "Postage stamp", "width": 0.05, "color": "#ffffff"},
    "circle": {"name": "Circle", "width": 0.0, "color": "#1d1b19"},
    "vignette": {"name": "Vignette", "width": 0.04, "color": "#000000"},
}
FRAME_ALIASES = {"passe-partout": "mat", "passe_partout": "mat", "instant": "polaroid", "film-strip": "film"}
MAX_FRAME_WIDTH = 0.5
VIGNETTE_BAND = 3.5     # the vignette's soft edge spans this many frame widths


def _rgba_canvas(size: tuple[int, int], color: tuple[int, int, int, int]) -> Image.Image:
    return Image.new("RGBA", size, color)


def _paste_center(canvas: Image.Image, img: Image.Image, offset: tuple[int, int]) -> Image.Image:
    layer = img.convert("RGBA")
    canvas.alpha_composite(layer, offset)
    return canvas


def _round_mask(size: tuple[int, int], radius: int, scale: int = 4) -> Image.Image:
    w, h = size
    radius = max(0, min(radius, min(w, h) // 2))
    if radius == 0:
        return Image.new("L", size, 255)
    # Supersample only the corner tiles: draw one corner at 4× and mirror it.
    r4 = radius * scale
    corner = Image.new("L", (r4, r4), 0)
    ImageDraw.Draw(corner).pieslice((0, 0, 2 * r4 - 1, 2 * r4 - 1), 180, 270, fill=255)
    corner = corner.resize((radius, radius), Image.Resampling.LANCZOS)
    mask = Image.new("L", size, 255)
    mask.paste(corner, (0, 0))
    mask.paste(corner.transpose(Image.Transpose.FLIP_LEFT_RIGHT), (w - radius, 0))
    mask.paste(corner.transpose(Image.Transpose.FLIP_TOP_BOTTOM), (0, h - radius))
    mask.paste(corner.transpose(Image.Transpose.ROTATE_180), (w - radius, h - radius))
    return mask


def _ellipse_mask(size: tuple[int, int]) -> Image.Image:
    w, h = size
    scale = 4 if w * h <= 4_000_000 else 2 if w * h <= 16_000_000 else 1
    big = Image.new("L", (w * scale, h * scale), 0)
    ImageDraw.Draw(big).ellipse((0, 0, w * scale - 1, h * scale - 1), fill=255)
    return big.resize(size, Image.Resampling.LANCZOS) if scale > 1 else big


def _with_mask(img: Image.Image, mask: Image.Image, fill: tuple[int, int, int, int]) -> Image.Image:
    rgba = img.convert("RGBA")
    if fill[3] == 0:
        alpha = ImageChops.multiply(rgba.getchannel("A"), mask)
        rgba.putalpha(alpha)
        return rgba
    canvas = Image.new("RGBA", rgba.size, fill)
    canvas.paste(rgba, (0, 0), ImageChops.multiply(rgba.getchannel("A"), mask))
    return canvas


def _check_frame_size(w: float, h: float) -> None:
    ops._check_output_size(int(math.ceil(w)), int(math.ceil(h)))  # noqa: SLF001 — HEYP_MAX_PIXELS


def apply_frame(img: Image.Image, style: str, color: Any = None, width: float | None = None,
                background: Any = None) -> Image.Image:
    """Add a frame. ``width`` is the border as a fraction of the shorter side (style default when None);
    ``color`` the frame colour (style default when None); ``background`` the gap/paper colour for
    styles that have one (double, shadow, film). Most styles enlarge the canvas."""
    if style == "none":
        return img.copy()
    style = FRAME_ALIASES.get(style, style)
    spec = FRAMES.get(style)
    if spec is None:
        raise ValueError(f"Unknown frame {style!r} (choose from {', '.join(FRAMES)})")
    frac = spec["width"] if width is None else float(width)
    if not math.isfinite(frac) or not 0.0 <= frac <= MAX_FRAME_WIDTH:
        raise ValueError(f"frame width must be between 0 and {MAX_FRAME_WIDTH}")
    col = parse_color(spec["color"] if color is None else color)
    W, H = img.size
    short = min(W, H)
    b = max(1, int(round(frac * short))) if frac > 0 else 0

    if style == "line":
        return ops.pad(img, b, b, b, b, color=col) if b else img.copy()
    if style == "double":
        if not b:
            return img.copy()
        bg = parse_color("#ffffff" if background is None else background)
        thin, gap = max(1, round(b * 0.2)), max(1, round(b * 0.35))
        thick = max(1, b - thin - gap)
        out = ops.pad(img, thin, thin, thin, thin, color=col)
        out = ops.pad(out, gap, gap, gap, gap, color=bg)
        return ops.pad(out, thick, thick, thick, thick, color=col)
    if style == "polaroid":
        side = max(1, b)
        return ops.pad(img, side, side, int(round(side * 3.6)), side, color=col)
    if style == "mat":
        mat = max(2, b)
        bevel = max(1, round(mat * 0.06))
        out = ops.pad(img, bevel, bevel, bevel, bevel, color=(0x8A, 0x84, 0x78, 255))
        out = ops.pad(out, mat, mat, mat, mat, color=col)
        rule = max(1, round(mat * 0.12))
        return ops.pad(out, rule, rule, rule, rule, color=_INK + (255,))
    if style == "rounded":
        return _with_mask(img, _round_mask(img.size, b), col)
    if style == "vignette":
        return _soft_edge(img, max(1, round(b * VIGNETTE_BAND)) if b else 0, col)
    if style == "circle":
        # Ellipse inscribed in the image; outside is ``background`` (transparent by default) and a
        # ring of ``width`` in ``color`` is drawn on the edge.
        out = _with_mask(img, _ellipse_mask(img.size), (0, 0, 0, 0))
        if b and col[3]:
            out = Image.alpha_composite(out, _ring_layer(img.size, b, col))
        outside = parse_color("transparent" if background is None else background)
        if outside[3]:
            return Image.alpha_composite(Image.new("RGBA", img.size, outside), out)
        return out
    if style == "shadow":
        pad = max(2, b * 2)
        bg = parse_color("#ffffff" if background is None else background)
        _check_frame_size(W + 2 * pad, H + 2 * pad)
        shadow_col = col
        canvas = _rgba_canvas((W + 2 * pad, H + 2 * pad), bg)
        shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        dx, dy = max(1, round(b * 0.3)), max(1, round(b * 0.45))
        alpha = img.convert("RGBA").getchannel("A") if img.mode in ("RGBA", "LA") else Image.new("L", img.size, 255)
        tinted = Image.new("RGBA", img.size, shadow_col[:3] + (0,))
        tinted.putalpha(alpha.point(lambda v: v * shadow_col[3] // 255))
        shadow.alpha_composite(tinted, (pad + dx, pad + dy))
        shadow = shadow.filter(ImageFilter.GaussianBlur(max(1.0, b * 0.5)))
        canvas = Image.alpha_composite(canvas, shadow)
        return _paste_center(canvas, img, (pad, pad))
    if style == "film":
        band = max(8, b)
        side = max(2, round(band * 0.25))
        _check_frame_size(W + 2 * side, H + 2 * band)
        canvas = _rgba_canvas((W + 2 * side, H + 2 * band), col)
        hole_bg = parse_color("#ffffff" if background is None else background)
        draw = ImageDraw.Draw(canvas)
        hole_h = max(3, round(band * 0.42))
        hole_w = max(3, round(hole_h * 0.7))
        step = hole_w * 2
        y_top = (band - hole_h) // 2
        y_bottom = band + H + (band - hole_h) // 2
        radius = max(1, hole_w // 4)
        for x in range(side + step // 4, canvas.width - hole_w, step):
            for y in (y_top, y_bottom):
                draw.rounded_rectangle((x, y, x + hole_w - 1, y + hole_h - 1), radius=radius, fill=hole_bg)
        return _paste_center(canvas, img, (side, band))
    # stamp
    margin = max(4, b)
    _check_frame_size(W + 2 * margin, H + 2 * margin)
    canvas = _rgba_canvas((W + 2 * margin, H + 2 * margin), col)
    canvas = _paste_center(canvas, img, (margin, margin))
    return _perforate(canvas, max(2, round(margin * 0.32)))


def _soft_edge(img: Image.Image, band: int, color: tuple[int, int, int, int]) -> Image.Image:
    """Fade the picture into ``color`` over ``band`` px from every edge (rounded corners), row strips."""
    if band <= 0 or color[3] == 0:
        return img.copy()
    rgb, alpha = _split_alpha(img)
    W, H = rgb.size
    src = np.asarray(rgb)
    out = np.empty_like(src)
    col = np.asarray(color[:3], np.float32)
    strength = color[3] / 255.0
    xs = np.arange(W, dtype=np.float32)
    ax = np.clip((band - np.minimum(xs, W - 1 - xs)) / band, 0.0, 1.0)[None, :]
    rows = max(1, STRIP_PIXELS // max(1, W))
    for y0 in range(0, H, rows):
        ys = np.arange(y0, min(H, y0 + rows), dtype=np.float32)
        ay = np.clip((band - np.minimum(ys, H - 1 - ys)) / band, 0.0, 1.0)[:, None]
        t = np.clip(np.sqrt(ax * ax + ay * ay), 0.0, 1.0)
        t = (t * t * (3 - 2 * t) * strength)[..., None]          # smoothstep: no visible band edge
        block = src[y0:y0 + rows].astype(np.float32)
        out[y0:y0 + rows] = np.clip(block + (col - block) * t + 0.5, 0, 255).astype(np.uint8)
    result = Image.fromarray(out, "RGB")
    if alpha is not None:
        result.putalpha(alpha)
    return result


def frame_size(style: str, size: tuple[float, float], width: float | None = None) -> tuple[float, float]:
    """Upper bound of :func:`apply_frame`'s output size (planning and pixel checks before allocating)."""
    style = FRAME_ALIASES.get(style, style)
    spec = FRAMES.get(style)
    if spec is None or style in ("rounded", "circle", "vignette"):
        return size
    w, h = size
    b = (spec["width"] if width is None else float(width)) * min(w, h) + 1
    return w + 4 * b + 24, h + 6 * b + 24


def _ring_layer(size: tuple[int, int], width: int, color: tuple[int, int, int, int]) -> Image.Image:
    w, h = size
    scale = 4 if w * h <= 4_000_000 else 2 if w * h <= 16_000_000 else 1
    big = Image.new("L", (w * scale, h * scale), 0)
    ImageDraw.Draw(big).ellipse((0, 0, w * scale - 1, h * scale - 1), outline=255, width=max(1, width * scale))
    mask = big.resize(size, Image.Resampling.LANCZOS) if scale > 1 else big
    layer = Image.new("RGBA", size, color[:3] + (0,))
    layer.putalpha(mask.point(lambda v: v * color[3] // 255))
    return layer


def _perforate(img: Image.Image, radius: int) -> Image.Image:
    """Punch semicircular holes along every edge (postage-stamp perforation), transparent."""
    w, h = img.size
    mask = Image.new("L", (w, h), 255)
    draw = ImageDraw.Draw(mask)
    step = radius * 3
    for edge_len, horizontal in ((w, True), (h, False)):
        count = max(2, int(edge_len // step))
        spacing = edge_len / count
        for i in range(count + 1):
            c = i * spacing
            if horizontal:
                for cy in (0, h):
                    draw.ellipse((c - radius, cy - radius, c + radius, cy + radius), fill=0)
            else:
                for cx in (0, w):
                    draw.ellipse((cx - radius, c - radius, cx + radius, c + radius), fill=0)
    out = img.convert("RGBA")
    out.putalpha(ImageChops.multiply(out.getchannel("A"), mask))
    return out


# ------------------------------------------------------------------ stickers
STICKER_ID_RE = re.compile(r"[a-z0-9]+/[a-z0-9][a-z0-9-]{0,80}")
STICKER_CREDIT = "Emoji stickers: OpenMoji – the open-source emoji and icon project (CC BY-SA 4.0)"


@functools.lru_cache(maxsize=1)
def _sticker_index() -> dict[str, dict[str, Any]]:
    path = STICKER_DIR / "manifest.json"
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    index = {}
    for item in data.get("stickers", []):
        sid, file = str(item.get("id", "")), str(item.get("file", ""))
        if STICKER_ID_RE.fullmatch(sid) and file == f"{sid}.svg" and (STICKER_DIR / file).is_file():
            index[sid] = item
    return index


def has_sticker(sticker_id: str) -> bool:
    return isinstance(sticker_id, str) and sticker_id in _sticker_index()


@functools.lru_cache(maxsize=512)
def sticker_ratio(sticker_id: str) -> float:
    """Height / width of a bundled sticker (from its SVG size; 1.0 when unknown)."""
    from . import io as imgio
    from xml.etree import ElementTree as ET

    try:
        w, h = imgio.svg_size(ET.fromstring(sticker_svg(sticker_id).encode("utf-8")))
    except Exception:
        return 1.0
    return float(h / w) if w and h else 1.0


def stickers() -> list[dict[str, Any]]:
    """Sticker catalogue for the UI: id, name, category, source (heyp | openmoji), aspect ``ratio``
    (h/w) and the ``url`` that serves the SVG (app/tools/img_tools.py)."""
    return [{"id": s["id"], "name": s["name"], "category": s["category"], "source": s["source"],
             "ratio": round(sticker_ratio(s["id"]), 4), "url": f"/api/assets/stickers/{s['id']}.svg",
             **({"emoji": s["emoji"]} if s.get("emoji") else {})} for s in _sticker_index().values()]


def sticker_svg(sticker_id: str) -> str:
    """The SVG source of a bundled sticker (KeyError for unknown ids — never a path from the request)."""
    item = _sticker_index().get(sticker_id)
    if item is None:
        raise KeyError(sticker_id)
    return (STICKER_DIR / item["file"]).read_text("utf-8")


_sticker_cache: OrderedDict[tuple[str, int], Image.Image] = OrderedDict()
_sticker_lock = threading.Lock()
_STICKER_CACHE_PIXELS = 24_000_000


def _rasterize(svg: str, width: int) -> Image.Image:
    """resvg → RGBA for SVG we generated or ship ourselves (no sandbox needed: trusted input)."""
    from io import BytesIO

    from . import io as imgio

    png = imgio._resvg_png(svg, int(width), None)  # noqa: SLF001
    with Image.open(BytesIO(png), formats=["PNG"]) as im:
        im.load()
        return im.convert("RGBA")


def render_sticker(sticker_id: str, width: int) -> Image.Image:
    """RGBA raster of a bundled sticker at ``width`` px (cached)."""
    if not has_sticker(sticker_id):
        raise ValueError(f"Unknown sticker {sticker_id!r}")
    width = int(min(MAX_STICKER_PX, max(4, width)))
    key = (sticker_id, width)
    with _sticker_lock:
        cached = _sticker_cache.get(key)
        if cached is not None:
            _sticker_cache.move_to_end(key)
            return cached.copy()
    img = _rasterize(sticker_svg(sticker_id), width)
    with _sticker_lock:
        _sticker_cache[key] = img
        total = sum(v.width * v.height for v in _sticker_cache.values())
        while total > _STICKER_CACHE_PIXELS and len(_sticker_cache) > 1:
            _, old = _sticker_cache.popitem(last=False)
            total -= old.width * old.height
    return img.copy()


# ------------------------------------------------------------------ overlay
# Geometry follows the UI's editing layer (static/js/stagekit.js, overlay.js) so what the user placed
# lands where they saw it: x/y are the item's centre in fractions of the image, text ``size`` is the
# font size as a fraction of the image HEIGHT, sticker/shape ``w`` a fraction of the WIDTH and shape
# ``h`` of the HEIGHT, shape ``stroke_width`` and brush ``width`` fractions of the WIDTH; rotation is
# clockwise degrees about the centre.
def _field(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _opacity_layer(layer: Image.Image, opacity: float) -> Image.Image:
    if opacity >= 1.0:
        return layer
    alpha = layer.getchannel("A").point(lambda v: int(round(v * opacity)))
    layer.putalpha(alpha)
    return layer


def _check_layer(w: float, h: float) -> None:
    if not (math.isfinite(w) and math.isfinite(h)) or w * h > MAX_LAYER_PIXELS:
        raise ValueError("An overlay item is too large; make it smaller")


def _paint(attr: str, color: Any) -> str:
    r, g, b, a = parse_color(color)
    if a == 0:
        return f'{attr}="none"'
    out = f'{attr}="#{r:02x}{g:02x}{b:02x}"'
    if a < 255:
        out += f' {attr}-opacity="{a / 255:.4f}"'
    return out


def _svg_doc(width: int, height: int, body: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">{body}</svg>')


def _text_layer(item: Any, W: int, H: int) -> Image.Image:
    size_px = float(_field(item, "size", 0.08)) * H
    max_w = _field(item, "max_width")
    stroke = float(_field(item, "outline_width", 0.0) or 0.0)
    outline = _field(item, "outline")
    bg = _field(item, "background")
    _check_layer(size_px * 2, size_px * 2)
    return text.render(
        str(_field(item, "text", "")), font=_field(item, "font"), size=size_px, color=_field(item, "color", "#000000"),
        bold=bool(_field(item, "bold", False)), align=_field(item, "align", "center") or "center",
        max_width=float(max_w) * W if max_w else None, line_spacing=float(_field(item, "line_spacing", 1.15) or 1.15),
        stroke_color=outline if outline and stroke > 0 else None,
        stroke_width=int(round(stroke * size_px)) if outline else 0,
        background=bg if bg else None, padding=int(round(size_px * 0.25)) if bg else 0)


def _sticker_layer(item: Any, W: int, H: int) -> Image.Image:
    """The sticker fitted inside its ``w`` × ``h`` box (contain), or ``w`` wide when no height is given."""
    sid = str(_field(item, "sticker", None) or _field(item, "id", "") or "")
    ratio = sticker_ratio(sid) if has_sticker(sid) else 1.0
    width_px = float(_field(item, "w", 0.2)) * W
    box_h = _field(item, "h")
    if box_h:
        width_px = min(width_px, float(box_h) * H / max(ratio, 1e-6))
    _check_layer(width_px, width_px * ratio)
    layer = render_sticker(sid, int(round(max(4.0, width_px))))
    if _field(item, "flip", False):
        layer = layer.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    return layer


def shape_svg(shape: str, w: float, hh: float, *, color: Any = "#e0262e", fill: Any = None,
              stroke: float = 2.0) -> str:
    """SVG of a shape in a ``w`` × ``hh`` px box — the same geometry as the UI's ``shapeSvg``."""
    if shape not in SHAPES:
        raise ValueError(f"Unknown shape {shape!r} (choose from {', '.join(SHAPES)})")
    s = max(0.5, float(stroke))
    i = s / 2
    f = lambda v: f"{v:.2f}"  # noqa: E731
    if shape == "ellipse":
        el = (f'<ellipse cx="{f(w / 2)}" cy="{f(hh / 2)}" rx="{f(max(0.0, w / 2 - i))}" '
              f'ry="{f(max(0.0, hh / 2 - i))}"')
    elif shape == "line":
        el = f'<line x1="{f(i)}" y1="{f(hh / 2)}" x2="{f(w - i)}" y2="{f(hh / 2)}"'
    elif shape == "arrow":
        head = min(w * 0.35, max(hh, s * 4))
        el = (f'<path d="M{f(i)} {f(hh / 2)}H{f(w - i)}M{f(w - head)} {f(hh / 2 - head * 0.55)}L{f(w - i)} '
              f'{f(hh / 2)}L{f(w - head)} {f(hh / 2 + head * 0.55)}"')
    elif shape == "star":
        pts = []
        for k in range(10):
            a = -math.pi / 2 + k * math.pi / 5
            r = 0.4 if k % 2 else 1.0
            pts.append(f"{f(w / 2 + math.cos(a) * r * (w / 2 - i))},{f(hh / 2 + math.sin(a) * r * (hh / 2 - i) + hh * 0.04)}")
        el = f'<polygon points="{" ".join(pts)}"'
    elif shape == "heart":
        x = lambda u: f(i + u * (w - s))  # noqa: E731
        y = lambda v: f(i + v * (hh - s))  # noqa: E731
        el = (f'<path d="M{x(0.5)} {y(0.98)}C{x(0.12)} {y(0.7)} {x(0)} {y(0.45)} {x(0)} {y(0.28)}C{x(0)} {y(0.1)} '
              f'{x(0.14)} {y(0)} {x(0.28)} {y(0)}C{x(0.4)} {y(0)} {x(0.47)} {y(0.08)} {x(0.5)} {y(0.18)}C{x(0.53)} '
              f'{y(0.08)} {x(0.6)} {y(0)} {x(0.72)} {y(0)}C{x(0.86)} {y(0)} {x(1)} {y(0.1)} {x(1)} {y(0.28)}C{x(1)} '
              f'{y(0.45)} {x(0.88)} {y(0.7)} {x(0.5)} {y(0.98)}Z"')
    else:
        el = f'<rect x="{f(i)}" y="{f(i)}" width="{f(max(0.0, w - s))}" height="{f(max(0.0, hh - s))}"'
    filled = fill is not None and shape not in ("line", "arrow")
    paint = (_paint("fill", fill) if filled else 'fill="none"') + " " + _paint("stroke", color)
    return f'{el} {paint} stroke-width="{f(s)}" stroke-linecap="round" stroke-linejoin="round"/>'


def _shape_layer(item: Any, W: int, H: int) -> Image.Image:
    shape = _field(item, "shape", "rect")
    if shape not in SHAPES:
        raise ValueError(f"Unknown shape {shape!r} (choose from {', '.join(SHAPES)})")
    w, hh = max(1.0, float(_field(item, "w", 0.3)) * W), max(1.0, float(_field(item, "h", 0.2)) * H)
    _check_layer(w, hh)
    raw_sw = _field(item, "stroke_width", 0.008)
    stroke = (0.008 if raw_sw is None else float(raw_sw)) * W
    color = _field(item, "color", "#e0262e") or "#e0262e"
    if stroke <= 0:
        color = "transparent"
    wpx, hpx = max(1, math.ceil(w)), max(1, math.ceil(hh))
    body = shape_svg(shape, wpx, hpx, color=color, fill=_field(item, "fill"), stroke=max(0.5, stroke))
    return _rasterize(_svg_doc(wpx, hpx, body), wpx)


def _brush_layer(item: Any, W: int, H: int) -> tuple[Image.Image, tuple[int, int]]:
    """Stroke layer (round caps and joins, like the UI's SVG path) and its top-left canvas position."""
    points = [(float(p[0]) * W, float(p[1]) * H) for p in (_field(item, "points", []) or [])]
    if not points:
        return Image.new("RGBA", (1, 1), (0, 0, 0, 0)), (0, 0)
    width = max(0.5, float(_field(item, "width", 0.01)) * W)
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    pad = width / 2 + 2
    x0, y0 = math.floor(min(xs) - pad), math.floor(min(ys) - pad)
    x1, y1 = math.ceil(max(xs) + pad), math.ceil(max(ys) + pad)
    # Clip to the canvas (strokes may leave it); nothing outside is ever visible.
    cx0, cy0, cx1, cy1 = max(x0, 0), max(y0, 0), min(x1, W), min(y1, H)
    if cx1 <= cx0 or cy1 <= cy0:
        return Image.new("RGBA", (1, 1), (0, 0, 0, 0)), (0, 0)
    bw, bh = cx1 - cx0, cy1 - cy0
    _check_layer(bw, bh)
    d = " ".join(f"{'L' if k else 'M'}{x - cx0:.1f} {y - cy0:.1f}" for k, (x, y) in enumerate(points))
    if len(points) == 1:
        d += " l0.01 0"
    body = (f'<path d="{d}" fill="none" {_paint("stroke", _field(item, "color", "#e0262e") or "#e0262e")} '
            f'stroke-width="{width:.2f}" stroke-linecap="round" stroke-linejoin="round"/>')
    return _rasterize(_svg_doc(bw, bh, body), bw), (cx0, cy0)


def _composite(base: Image.Image, layer: Image.Image, left: int, top: int, blend: str = "normal") -> None:
    """Alpha-composite ``layer`` at (left, top) onto RGBA ``base`` in place, clipped to the canvas."""
    W, H = base.size
    x0, y0 = max(0, left), max(0, top)
    x1, y1 = min(W, left + layer.width), min(H, top + layer.height)
    if x1 <= x0 or y1 <= y0:
        return
    piece = layer.crop((x0 - left, y0 - top, x1 - left, y1 - top))
    if blend == "multiply":
        region = base.crop((x0, y0, x1, y1))
        multiplied = ImageChops.multiply(region.convert("RGB"), piece.convert("RGB"))
        multiplied.putalpha(region.getchannel("A"))
        mixed = Image.composite(multiplied, region, piece.getchannel("A"))
        base.paste(mixed, (x0, y0))
        return
    base.alpha_composite(piece, (x0, y0))


def _check_items(items: Sequence[Any]) -> None:
    if len(items) > MAX_ITEMS:
        raise ValueError(f"Too many overlay items ({len(items)}, max {MAX_ITEMS})")
    points = sum(len(_field(i, "points", []) or []) for i in items if _field(i, "type") == "brush")
    if points > MAX_BRUSH_POINTS:
        raise ValueError(f"Too many brush points ({points}, max {MAX_BRUSH_POINTS}); simplify the drawing")


@dataclass
class Placed:
    """A rendered overlay item: its RGBA layer, top-left canvas position and blend mode."""

    layer: Image.Image
    left: int
    top: int
    blend: str = "normal"


def prepare_overlay(items: Sequence[Any], size: tuple[int, int],
                    check_cancel: Callable[[], None] | None = None) -> list[Placed]:
    """Render every item for a canvas of ``size`` once (animations composite the result on each frame).

    Items (pydantic models or dicts) have ``type`` text | sticker | shape | brush, ``x``/``y`` centre,
    ``rotation`` and ``opacity``; see the geometry note above.
    """
    _check_items(items)
    W, H = size
    placed: list[Placed] = []
    for item in items:
        if check_cancel is not None:
            check_cancel()
        kind = _field(item, "type")
        raw_opacity = _field(item, "opacity", 1.0)
        opacity = 1.0 if raw_opacity is None else float(raw_opacity)
        if not 0.0 <= opacity <= 1.0:
            raise ValueError("opacity must be between 0 and 1")
        if opacity == 0.0:
            continue
        rotation = float(_field(item, "rotation", 0.0) or 0.0)
        if kind == "brush":
            layer, (left, top) = _brush_layer(item, W, H)
            blend = "multiply" if _field(item, "mode", "pen") == "highlighter" else "normal"
            placed.append(Placed(_opacity_layer(layer, opacity), left, top, blend))
            continue
        if kind == "text":
            layer = _text_layer(item, W, H)
        elif kind == "sticker":
            layer = _sticker_layer(item, W, H)
        elif kind == "shape":
            layer = _shape_layer(item, W, H)
        else:
            raise ValueError(f"Unknown overlay item type {kind!r} (use text, sticker, shape or brush)")
        centre = (float(_field(item, "x", 0.5)) * W, float(_field(item, "y", 0.5)) * H)
        if rotation:
            layer = text.rotate_rgba(layer, rotation)
        layer = _opacity_layer(layer, opacity)
        placed.append(Placed(layer, int(round(centre[0] - layer.width / 2)),
                             int(round(centre[1] - layer.height / 2))))
    return placed


def composite_overlay(img: Image.Image, placed: Sequence[Placed]) -> Image.Image:
    """Draw prepared items onto ``img``; RGB stays RGB, images with alpha stay RGBA."""
    if not placed:
        return img.copy()
    had_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    base = img.convert("RGBA")
    for item in placed:
        _composite(base, item.layer, item.left, item.top, item.blend)
    return base if had_alpha else base.convert("RGB")


def apply_overlay(img: Image.Image, items: Sequence[Any]) -> Image.Image:
    """Draw overlay items (pydantic models or dicts) in order onto the image (see prepare_overlay)."""
    return composite_overlay(img, prepare_overlay(items, img.size))


# ------------------------------------------------------------------ catalogue for the UI
def catalog() -> dict[str, Any]:
    """Everything the editor UI lists: filters, frames, fonts, shapes, stickers (+ credit line)."""
    return {
        "filters": [{"id": f.id, "label": f.name, "name": f.name} for f in FILTERS.values()],
        "frames": [{"id": k, "label": v["name"], "name": v["name"], "width": v["width"], "color": v["color"]}
                   for k, v in FRAMES.items()],
        "fonts": text.list_fonts(),
        "shapes": list(SHAPES),
        "stickers": stickers(),
        "sticker_credit": STICKER_CREDIT,
        "limits": {"max_items": MAX_ITEMS, "max_brush_points": MAX_BRUSH_POINTS},
    }
