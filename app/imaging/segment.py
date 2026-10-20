"""Person segmentation with U²-Net (human_seg) on onnxruntime, plus background compositing."""
from __future__ import annotations

import importlib.util
import os
import threading
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageColor

MODEL_FILE = "u2net_human_seg.onnx"
INPUT_NAME = "input.1"
INPUT_SIZE = 320
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

_lock = threading.Lock()
_session = None
_session_failed = False


def _model_path() -> Path:
    return Path(os.environ.get("HEYP_MODELS_DIR", "/opt/heyp/models")) / MODEL_FILE


def available() -> bool:
    """True when the U²-Net model file exists and onnxruntime is importable (cheap check)."""
    if _session is not None:
        return True
    if _session_failed:
        return False
    if importlib.util.find_spec("onnxruntime") is None:
        return False
    path = _model_path()
    return path.is_file() and path.stat().st_size > 1_000_000


def _get_session():
    """Create the ONNX session once (thread-safe); raises RuntimeError when unavailable."""
    global _session, _session_failed
    if _session is not None:
        return _session
    with _lock:
        if _session is None:
            if not available():
                raise RuntimeError("person segmentation is unavailable (U²-Net model not installed)")
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.intra_op_num_threads = min(4, os.cpu_count() or 1)
            opts.inter_op_num_threads = 1
            opts.log_severity_level = 3
            try:
                _session = ort.InferenceSession(str(_model_path()), sess_options=opts,
                                                providers=["CPUExecutionProvider"])
            except Exception as exc:  # corrupt/incompatible model file
                _session_failed = True
                raise RuntimeError(f"could not load the segmentation model: {exc}") from exc
    return _session


def _rgb_on_white(img: Image.Image) -> Image.Image:
    if img.mode == "RGB":
        return img
    if "A" in img.getbands() or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    return img.convert("RGB")


def _resize_float(arr: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Bilinear resize of a float32 HxW array to (W, H) without OpenCV's 32k side limit."""
    if (arr.shape[1], arr.shape[0]) == tuple(size):
        return arr.astype(np.float32, copy=False)
    out = Image.fromarray(np.ascontiguousarray(arr, dtype=np.float32)).resize(size, Image.Resampling.BILINEAR)
    return np.asarray(out, dtype=np.float32)


def raw_mask(img: Image.Image) -> np.ndarray:
    """U²-Net prediction at 320×320, min-max normalised to [0, 1] (float32)."""
    session = _get_session()
    small = img
    if small.mode not in ("RGB", "RGBA", "L", "LA"):
        small = small.convert("RGBA" if "transparency" in small.info else "RGB")
    # Resize first, then flatten: never copies a huge image at full size.
    rgb = _rgb_on_white(small.resize((INPUT_SIZE, INPUT_SIZE), Image.Resampling.LANCZOS, reducing_gap=3.0))
    x = np.asarray(rgb, dtype=np.float32)
    x = x / max(float(x.max()), 1e-6)  # rembg-style: divide by the max, then ImageNet mean/std
    x = (x - _MEAN) / _STD
    x = np.ascontiguousarray(x.transpose(2, 0, 1)[None], dtype=np.float32)
    pred = session.run(None, {INPUT_NAME: x})[0][:, 0][0].astype(np.float32)
    lo, hi = float(pred.min()), float(pred.max())
    if hi - lo < 1e-6:
        return np.zeros_like(pred)
    return (pred - lo) / (hi - lo)


def _blur_sigma(size: tuple[int, int]) -> float:
    # ~1 px at preview scale, growing with resolution so the 320 px grid never shows.
    return max(0.8, max(size) / 1600.0)


def person_mask(img: Image.Image, *, size: tuple[int, int] | None = None) -> np.ndarray:
    """Person probability mask, float32 HxW in [0, 1].

    The mask matches `img.size` unless `size=(W, H)` asks for another resolution
    (e.g. a reduced analysis mask for a very large image).
    """
    if not isinstance(img, Image.Image):
        raise TypeError("person_mask() expects a PIL image")
    out_w, out_h = size or img.size
    if out_w < 1 or out_h < 1:
        raise ValueError("mask size must be at least 1×1")
    mask = _resize_float(raw_mask(img), (out_w, out_h))
    sigma = _blur_sigma((out_w, out_h))
    mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=sigma, sigmaY=sigma, borderType=cv2.BORDER_REPLICATE)
    return np.clip(mask, 0.0, 1.0, out=mask)


def parse_color(color: str | tuple | None) -> tuple[int, int, int, int]:
    """CSS colour / "#rrggbb[aa]" / "transparent" → RGBA tuple."""
    if color is None:
        return 255, 255, 255, 255
    if isinstance(color, (tuple, list)):
        vals = tuple(int(v) for v in color)
        if len(vals) == 3:
            vals = (*vals, 255)
        if len(vals) != 4 or not all(0 <= v <= 255 for v in vals):
            raise ValueError(f"invalid colour {color!r}")
        return vals  # type: ignore[return-value]
    text = str(color).strip().lower()
    if text in ("transparent", "none"):
        return 0, 0, 0, 0
    try:
        rgb = ImageColor.getrgb(text)
    except ValueError:
        raise ValueError(f"invalid colour {color!r}") from None
    return (*rgb, 255) if len(rgb) == 3 else rgb  # type: ignore[return-value]


_DECON_MAX_SIDE = 768  # colour fields are smooth, so estimate them on a small copy


def _push_pull(rgb: np.ndarray, weight: np.ndarray) -> np.ndarray:
    """Fill every pixel with the colour of the nearest "known" pixels (weight > 0).

    Classic push-pull: average known colours down an image pyramid, then pull coarse estimates
    back up into cells that had no known pixels. Known pixels keep their own colour.
    """
    levels = []
    c, w = rgb * weight[..., None], weight
    while True:
        levels.append((c, w))
        h_, w_ = w.shape
        if max(h_, w_) <= 2:
            break
        size = (max(1, (w_ + 1) // 2), max(1, (h_ + 1) // 2))
        c = cv2.resize(c, size, interpolation=cv2.INTER_AREA)
        w = cv2.resize(w, size, interpolation=cv2.INTER_AREA)
    est = None
    for c, w in reversed(levels):
        local = c / np.maximum(w, 1e-6)[..., None]
        if est is None:
            est = local
            continue
        up = cv2.resize(est, (w.shape[1], w.shape[0]), interpolation=cv2.INTER_LINEAR)
        k = np.clip(w * 4.0, 0.0, 1.0)[..., None]
        est = k * local + (1.0 - k) * up
    return est.astype(np.float32)


def _sample(field: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Bilinear lookup of an HxWxC float field at float coordinates → N×C."""
    n = len(xs)
    cols = min(n, 4096)  # cv2.remap needs map sides < 32767
    rows = -(-n // cols)
    mx = np.zeros(rows * cols, np.float32)
    my = np.zeros(rows * cols, np.float32)
    mx[:n], my[:n] = xs, ys
    out = cv2.remap(field, mx.reshape(rows, cols), my.reshape(rows, cols),
                    interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return out.reshape(rows * cols, -1)[:n]


def _decontaminate(rgb: np.ndarray, alpha: np.ndarray) -> None:
    """Reduce colour spill of the old background in semi-transparent pixels (in place, uint8 rgb).

    Model C = a·F + (1−a)·B, with B / F taken from the nearest solid background / foreground
    (push-pull fill). Near-opaque pixels are solved for F; lower-alpha pixels take the nearby
    solid foreground colour, which also hides a mask that reaches slightly into the background.
    Only edge pixels are touched and the colour fields are built on a ≤768 px copy.
    """
    ys, xs = np.nonzero((alpha > 0.02) & (alpha < 0.98))
    if ys.size == 0:
        return
    h, w = alpha.shape
    f = min(1.0, _DECON_MAX_SIDE / max(h, w))
    sw, sh = max(1, round(w * f)), max(1, round(h * f))
    small_rgb = cv2.resize(rgb, (sw, sh), interpolation=cv2.INTER_AREA).astype(np.float32)
    small_a = cv2.resize(alpha, (sw, sh), interpolation=cv2.INTER_AREA)
    fg_known = np.clip((small_a - 0.9) / 0.06, 0.0, 1.0)
    bg_known = np.clip((0.1 - small_a) / 0.06, 0.0, 1.0)
    if not fg_known.any() or not bg_known.any():
        return
    bg_field = _push_pull(small_rgb, bg_known)
    fg_field = _push_pull(small_rgb, fg_known)
    sx = (xs + 0.5) * (sw / w) - 0.5
    sy = (ys + 0.5) * (sh / h) - 0.5
    bg_est, fg_est = _sample(bg_field, sx, sy), _sample(fg_field, sx, sy)

    c = rgb[ys, xs].astype(np.float32)
    a = alpha[ys, xs][:, None]
    solved = np.clip((c - (1.0 - a) * bg_est) / np.maximum(a, 1e-3), 0.0, 255.0)
    t = np.clip((a - 0.5) / 0.45, 0.0, 1.0)
    t = t * t * (3.0 - 2.0 * t)  # smoothstep: trust the solve only where alpha is large enough
    rgb[ys, xs] = np.clip(t * solved + (1.0 - t) * fg_est + 0.5, 0, 255).astype(np.uint8)


def composite(img: Image.Image, mask: np.ndarray, color: str | tuple = "#ffffff", *,
              feather: float = 1.5, decontaminate: bool = True) -> Image.Image:
    """Put the masked subject over `color`; "transparent" (or any alpha < 255) gives RGBA.

    `mask` is float HxW in [0, 1] matching `img`; it is feathered by `feather` px (Gaussian sigma).
    An existing alpha channel in `img` is respected. Fully transparent output pixels are zeroed
    so the removed background cannot be recovered from the RGB channels.
    """
    mask = np.asarray(mask, dtype=np.float32)
    if mask.shape != (img.height, img.width):
        raise ValueError(f"mask shape {mask.shape} does not match image size {img.width}×{img.height}")
    if feather < 0:
        raise ValueError("feather must be >= 0")
    bg = parse_color(color)
    alpha = np.clip(mask, 0.0, 1.0)
    if feather > 0:
        alpha = cv2.GaussianBlur(alpha, (0, 0), feather, borderType=cv2.BORDER_REPLICATE)
    if "A" in img.getbands() or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        alpha = alpha * (np.asarray(rgba.getchannel("A"), dtype=np.float32) / 255.0)
        rgb = np.array(rgba.convert("RGB"), dtype=np.uint8)
    else:
        rgb = np.array(img.convert("RGB"), dtype=np.uint8)
    if decontaminate:
        _decontaminate(rgb, alpha)

    alpha_l = Image.fromarray(np.clip(alpha * 255.0 + 0.5, 0, 255).astype(np.uint8))
    fg = Image.fromarray(rgb)
    if bg[3] == 255:
        return Image.composite(fg, Image.new("RGB", fg.size, bg[:3]), alpha_l)
    fg.putalpha(alpha_l)
    if bg[3] > 0:
        return Image.alpha_composite(Image.new("RGBA", fg.size, bg), fg)
    arr = np.array(fg)
    arr[arr[..., 3] == 0] = 0
    return Image.fromarray(arr)


def remove_background(img: Image.Image) -> Image.Image:
    """Cut out the person; returns RGBA with a transparent background."""
    return composite(img, person_mask(img), "transparent")


def replace_background(img: Image.Image, color: str | tuple = "#ffffff") -> Image.Image:
    """Put the person on a solid colour; RGB (RGBA if `color` is transparent)."""
    return composite(img, person_mask(img), color)


__all__ = ["available", "person_mask", "raw_mask", "composite", "remove_background",
           "replace_background", "parse_color"]
