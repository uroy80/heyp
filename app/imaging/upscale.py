"""AI upscaling 2×/4× with Real-ESRGAN realesr-general-x4v3 (BSD-3-Clause) on onnxruntime.

Two exports of the same 4.9 MB compact network (``scripts/export_models/``):
"balanced" (default) blends the denoising weights 50/50 with ``wdn-x4v3`` — Real-ESRGAN's own
default, keeps a little natural texture — and "strong" is pure x4v3 (cleaner, smoother).

The image is processed in tiles of at most 256 px with 16 px of real (or mirrored) context on every
side, so seams never show and memory stays flat (~0.2–0.6 GB) whatever the image size. 2× runs the
4× model and halves each padded tile with an area filter before cropping, so no 4× frame is ever
held. Alpha is upscaled separately with Lanczos; before the colour goes through the network, fully
transparent pixels take the colour of the nearest visible one (``fill_hidden``), so the result never depends
on the invisible colour an exporter left under alpha 0 (often black: a dark fringe on light backgrounds).
Cost: ~11 s per input megapixel on 4 cores.
"""
from __future__ import annotations

import math
import os
from collections.abc import Callable

import cv2
import numpy as np
from PIL import Image

from app.imaging.ortsession import ModelSession, release_memory

MODEL_FILES = {"balanced": "realesr-general-x4v3-dn05.onnx", "strong": "realesr-general-x4v3.onnx"}
MODELS = tuple(MODEL_FILES)
FACTORS = (2, 4)
NET_SCALE = 4
TILE = 256        # max tile side (input px); research: 256 + 16 px context, no visible seams
CONTEXT = 16      # px of context around every tile
SECONDS_PER_MP = 11.0  # measured at 4 threads (M2 Pro); ~21 s at 2 threads
# Inputs above this many megapixels are refused (HEYP_UPSCALE_MAX_INPUT_MP overrides): ~1 min at 4 threads.
DEFAULT_MAX_INPUT_MP = 6.0

_sessions = {name: ModelSession(file, min_bytes=1_000_000, label=f"upscale ({name})")
             for name, file in MODEL_FILES.items()}


class LimitError(Exception):
    """Input/output too large for upscaling (HTTP 413)."""

    status = 413


def available(model: str = "balanced") -> bool:
    session = _sessions.get(model)
    return bool(session and session.available())


def max_input_pixels() -> int:
    """HEYP_UPSCALE_MAX_INPUT_MP (megapixels, may be fractional), else DEFAULT_MAX_INPUT_MP."""
    raw = os.environ.get("HEYP_UPSCALE_MAX_INPUT_MP", "").strip()
    try:
        mp = float(raw) if raw else DEFAULT_MAX_INPUT_MP
    except ValueError:
        mp = DEFAULT_MAX_INPUT_MP
    if not math.isfinite(mp) or mp <= 0:
        mp = DEFAULT_MAX_INPUT_MP
    return int(mp * 1_000_000)


def check_limits(size: tuple[int, int], factor: int, max_output_pixels: int) -> None:
    """Raise LimitError (413) when the input or the result would be too large; ValueError on a bad factor."""
    w, h = size
    if factor not in FACTORS:
        raise ValueError(f"upscale factor must be one of {FACTORS}")
    limit_in = max_input_pixels()
    if w * h > limit_in:
        raise LimitError(f"Upscaling works on images up to {limit_in / 1e6:.1f} MP (this one is "
                         f"{w * h / 1e6:.1f} MP); crop or downsize it first")
    out = w * factor * h * factor
    if out > max_output_pixels:
        side = math.sqrt(max_output_pixels / (factor * factor) * w / h)
        raise LimitError(f"A {factor}× upscale of this {w}×{h} image would be {out / 1e6:.0f} MP, above the "
                         f"{max_output_pixels / 1e6:.0f} MP limit; use 2× or make it at most "
                         f"{int(side)}×{int(side * h / w)} first")


def _grid(n: int) -> tuple[int, int]:
    """(tile count, tile size) covering ``n`` px with equal tiles of at most TILE px."""
    count = max(1, math.ceil(n / TILE))
    return count, math.ceil(n / count)


def _pad(arr: np.ndarray, top: int, bottom: int, left: int, right: int) -> np.ndarray:
    # Mirror real content into the margins (as Real-ESRGAN's own tiling does); a 1-px side can't mirror.
    mode = "reflect" if min(arr.shape[0], arr.shape[1]) > 1 else "edge"
    return np.pad(arr, ((top, bottom), (left, right), (0, 0)), mode=mode)


def upscale_rgb(rgb: np.ndarray, factor: int, model: str = "balanced", *,
                progress: Callable[[float], None] | None = None,
                check_cancel: Callable[[], None] | None = None) -> np.ndarray:
    """HxWx3 uint8 → (H·f)x(W·f)x3 uint8 with the ONNX model, tile by tile."""
    if factor not in FACTORS:
        raise ValueError(f"upscale factor must be one of {FACTORS}")
    session = _sessions.get(model)
    if session is None:
        raise ValueError(f"unknown upscale model {model!r} (choose from {', '.join(MODELS)})")
    if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.dtype != np.uint8:
        raise ValueError("upscale_rgb expects an HxWx3 uint8 array")
    h, w = rgb.shape[:2]
    ny, th = _grid(h)
    nx, tw = _grid(w)
    p = CONTEXT
    padded = _pad(rgb, p, p + ny * th - h, p, p + nx * tw - w)
    out = np.empty((h * factor, w * factor, 3), np.uint8)
    name = session.input_name()
    k = NET_SCALE // factor  # output px per tile px after the optional halving: 4 → 1, 2 → 2
    total, done = ny * nx, 0
    try:
        for iy in range(ny):
            for ix in range(nx):
                if check_cancel is not None:
                    check_cancel()
                y0, x0 = iy * th, ix * tw
                tile = padded[y0:y0 + th + 2 * p, x0:x0 + tw + 2 * p]
                x = np.ascontiguousarray(tile.transpose(2, 0, 1)[None], dtype=np.float32)
                x *= 1.0 / 255.0
                y = session.run({name: x})[0][0]  # 3 x 4(th+2p) x 4(tw+2p), float32 ~[0, 1]
                y = y.transpose(1, 2, 0)
                if factor != NET_SCALE:
                    y = cv2.resize(y, (y.shape[1] // k, y.shape[0] // k), interpolation=cv2.INTER_AREA)
                s = factor
                cy, cx = min(th, h - y0), min(tw, w - x0)  # the last row/column of tiles may overhang
                crop = y[p * s:p * s + cy * s, p * s:p * s + cx * s]
                out[y0 * s:(y0 + cy) * s, x0 * s:(x0 + cx) * s] = np.clip(crop * 255.0 + 0.5, 0, 255).astype(np.uint8)
                done += 1
                if progress is not None:
                    progress(done / total)
    finally:
        release_memory()
    return out


def fill_hidden(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """``rgb`` with every fully transparent pixel set to the colour of the nearest visible (alpha > 0) one.

    The network has a receptive field of several pixels, so the colour stored under alpha 0 leaks into the
    semi-transparent edge it is drawn next to. Extending the visible edge colour outwards makes the input
    (and so the result) a function of the visible image alone. A fully transparent image becomes black.
    """
    hidden = alpha == 0
    if not hidden.any():
        return rgb
    if hidden.all():
        return np.zeros_like(rgb)
    # Every visible pixel is a zero of the mask and gets its own label; each hidden pixel gets the label
    # of the visible pixel nearest to it.
    _, labels = cv2.distanceTransformWithLabels(hidden.view(np.uint8), cv2.DIST_L2, 5,
                                                labelType=cv2.DIST_LABEL_PIXEL)
    visible = ~hidden
    colours = np.zeros((int(labels.max()) + 1, 3), np.uint8)
    colours[labels[visible]] = rgb[visible]
    return colours[labels]


def upscale(img: Image.Image, factor: int = 2, model: str = "balanced", *,
            progress: Callable[[float], None] | None = None,
            check_cancel: Callable[[], None] | None = None) -> Image.Image:
    """Upscale a PIL image 2× or 4×; keeps L/LA/RGB/RGBA (other modes become RGB/RGBA).

    Callers enforce size limits first (``check_limits``); raises RuntimeError when the model is
    missing or broken, ValueError on bad arguments.
    """
    if not isinstance(img, Image.Image):
        raise TypeError("upscale() expects a PIL image")
    if img.mode not in ("RGB", "RGBA", "L", "LA"):
        alpha = "A" in img.getbands() or "transparency" in img.info
        img = img.convert("RGBA" if alpha else "RGB")
    gray = img.mode in ("L", "LA")
    alpha = img.getchannel("A") if img.mode in ("RGBA", "LA") else None
    rgb = np.asarray(img.convert("RGB"))
    if alpha is not None:
        rgb = fill_hidden(rgb, np.asarray(alpha))
    up = Image.fromarray(upscale_rgb(rgb, factor, model, progress=progress, check_cancel=check_cancel))
    del rgb
    if gray:
        up = up.convert("L")
    if alpha is not None:
        up.putalpha(alpha.resize(up.size, Image.Resampling.LANCZOS))
    return up


def estimate_seconds(size: tuple[int, int], threads: int = 4) -> float:
    """Rough wall time on the reference machine (for UI hints / job timeouts)."""
    mp = size[0] * size[1] / 1e6
    return SECONDS_PER_MP * mp * max(1.0, 4 / max(1, threads)) ** 0.9


__all__ = ["available", "upscale", "upscale_rgb", "fill_hidden", "check_limits", "max_input_pixels", "LimitError",
           "MODELS", "FACTORS", "MODEL_FILES", "estimate_seconds"]
