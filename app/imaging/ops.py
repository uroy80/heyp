"""Pure image operations, Image → Image (PLAN.md §3 "Ops", §4).

Every function validates its arguments and raises ``ValueError`` with a user-facing message on
bad input (the API maps that to 422). Inputs are expected in the normalised modes produced by
``io.load`` (RGB, RGBA, L, LA); other modes are converted where needed.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

import numpy as np
from PIL import Image, ImageChops, ImageColor, ImageEnhance, ImageOps

__all__ = [
    "parse_color", "parse_ratio", "rotate", "flip", "crop", "crop_aspect", "trim", "resize", "pad",
    "adjust", "smart_box", "to_px", "RESIZE_MODES", "UNITS", "RESAMPLE",
]

RESIZE_MODES = ("fit", "fill", "pad", "exact", "scale", "long_edge", "short_edge", "megapixels")
UNITS = ("px", "in", "cm", "mm")
_UNIT_PER_INCH = {"in": 1.0, "cm": 2.54, "mm": 25.4}
RESAMPLE = {
    "lanczos": Image.Resampling.LANCZOS,
    "bicubic": Image.Resampling.BICUBIC,
    "bilinear": Image.Resampling.BILINEAR,
    "nearest": Image.Resampling.NEAREST,
}
_REDUCING_GAP = 3.0  # Pillow: >= 3 is indistinguishable from fair resampling, much faster on big downscales
_MAX_DPI = 10_000
_SALIENCY_SIDE = 256


# ------------------------------------------------------------------ parsing helpers
def parse_color(s: Any) -> tuple[int, int, int, int]:
    """Parse ``#rgb``, ``#rgba``, ``#rrggbb``, ``#rrggbbaa``, CSS names/``rgb()``, ``transparent`` → RGBA."""
    if isinstance(s, (tuple, list)) and len(s) in (3, 4):
        try:
            values = [int(v) for v in s]
        except (TypeError, ValueError):
            raise ValueError(f"Invalid colour: {s!r}") from None
        if any(v < 0 or v > 255 for v in values):
            raise ValueError(f"Invalid colour: {s!r} (channels must be 0–255)")
        if len(values) == 3:
            values.append(255)
        return values[0], values[1], values[2], values[3]
    if not isinstance(s, str) or not s.strip():
        raise ValueError(f"Invalid colour: {s!r}")
    text = s.strip().lower()
    if text in ("transparent", "none"):
        return 0, 0, 0, 0
    try:
        rgb = ImageColor.getrgb(text)
    except ValueError:
        raise ValueError(f"Invalid colour: {s!r} (use #rrggbb, #rrggbbaa, a CSS name or 'transparent')") from None
    if len(rgb) == 3:
        return rgb[0], rgb[1], rgb[2], 255
    return rgb[0], rgb[1], rgb[2], rgb[3]


def parse_ratio(r: Any) -> float:
    """Parse an aspect ratio (width / height): ``"16:9"``, ``"4/5"``, ``"3x2"``, ``"1.5"`` or a number."""
    if isinstance(r, bool):
        raise ValueError(f"Invalid aspect ratio: {r!r}")
    try:
        if isinstance(r, (int, float)):
            value = float(r)
        elif isinstance(r, str):
            text = r.strip().lower()
            for sep in (":", "/", "x", "×"):
                if sep in text:
                    num, den = text.split(sep, 1)
                    value = float(num) / float(den)
                    break
            else:
                value = float(text)
        else:
            raise ValueError
    except (ValueError, ZeroDivisionError):
        raise ValueError(f"Invalid aspect ratio: {r!r} (use e.g. '16:9', '4/5' or 1.5)") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"Invalid aspect ratio: {r!r}")
    if not 0.01 <= value <= 100:
        raise ValueError(f"Aspect ratio {r!r} is out of range (0.01–100)")
    return value


def to_px(value: float, unit: str, dpi: float) -> int:
    """Convert a length in ``unit`` (px/in/cm/mm) to whole pixels at ``dpi``."""
    if unit not in UNITS:
        raise ValueError(f"Unknown unit {unit!r} (use one of {', '.join(UNITS)})")
    value = _finite(value, "value")
    if unit == "px":
        return int(round(value))
    dpi = _check_dpi(dpi)
    return int(round(value / _UNIT_PER_INCH[unit] * dpi))


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _positive(value: Any, name: str) -> float:
    number = _finite(value, name)
    if number <= 0:
        raise ValueError(f"{name} must be greater than 0")
    return number


def _check_dpi(dpi: Any) -> float:
    value = _positive(dpi, "dpi")
    if value > _MAX_DPI:
        raise ValueError(f"dpi must be at most {_MAX_DPI}")
    return value


def _check_output_size(width: int, height: int) -> None:
    """Refuse results above the decompression-bomb limit (HEYP_MAX_PIXELS)."""
    from .io import max_pixels  # lazy: io imports this module

    limit = max_pixels()
    if width * height > limit:
        raise ValueError(
            f"Result would be {width}×{height} px ({width * height / 1e6:.1f} MP), "
            f"above the {limit / 1e6:.0f} MP limit"
        )


def _with_fill(img: Image.Image, color: Any) -> tuple[Image.Image, Any]:
    """Return ``img`` in a mode that can hold ``color`` plus the matching Pillow fill value."""
    r, g, b, a = parse_color(color)
    mode = img.mode
    if mode not in ("RGB", "RGBA", "L", "LA"):
        mode = "RGBA" if "A" in img.getbands() else "RGB"
    gray = r == g == b
    if mode in ("L", "LA") and not gray:
        mode = "RGBA" if (mode == "LA" or a < 255) else "RGB"
    if a < 255 and mode in ("RGB", "L"):
        mode = mode + "A" if mode == "L" else "RGBA"
    if mode != img.mode:
        img = img.convert(mode)
    fill = {"L": r, "LA": (r, a), "RGB": (r, g, b), "RGBA": (r, g, b, a)}[mode]
    return img, fill


# ------------------------------------------------------------------ geometry
def rotate(img: Image.Image, angle: float, expand: bool = True, fill: Any = "#ffffff") -> Image.Image:
    """Rotate CLOCKWISE by ``angle`` degrees; multiples of 90 are lossless transposes."""
    angle = _finite(angle, "angle") % 360.0
    quarter = round(angle / 90.0)
    if abs(angle - quarter * 90.0) < 1e-9:
        turns = quarter % 4
        if turns == 0:
            return img.copy()
        method = {1: Image.Transpose.ROTATE_270, 2: Image.Transpose.ROTATE_180, 3: Image.Transpose.ROTATE_90}[turns]
        return img.transpose(method)
    if expand:
        rad = math.radians(angle)
        cos, sin = abs(math.cos(rad)), abs(math.sin(rad))
        _check_output_size(math.ceil(img.width * cos + img.height * sin),
                           math.ceil(img.width * sin + img.height * cos))
    src, fill_value = _with_fill(img, fill)
    # PIL rotates counter-clockwise for positive angles.
    return src.rotate(-angle, resample=Image.Resampling.BICUBIC, expand=bool(expand), fillcolor=fill_value)


def flip(img: Image.Image, direction: str) -> Image.Image:
    """Mirror horizontally (left↔right) or vertically (top↔bottom)."""
    if direction == "horizontal":
        return img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if direction == "vertical":
        return img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    raise ValueError(f"Invalid flip direction {direction!r} (use 'horizontal' or 'vertical')")


def crop(img: Image.Image, x: float, y: float, width: float, height: float, unit: str = "px") -> Image.Image:
    """Crop a box given in px or as fractions of the current size; clamped to the image, ≥ 1 px."""
    x, y = _finite(x, "x"), _finite(y, "y")
    width, height = _positive(width, "width"), _positive(height, "height")
    W, H = img.size
    if unit == "fraction":
        left, top, right, bottom = x * W, y * H, (x + width) * W, (y + height) * H
    elif unit == "px":
        left, top, right, bottom = x, y, x + width, y + height
    else:
        raise ValueError(f"Invalid crop unit {unit!r} (use 'px' or 'fraction')")
    if right <= 0 or bottom <= 0 or left >= W or top >= H:
        raise ValueError(f"Crop box lies outside the image ({W}×{H})")
    left_i = min(max(int(round(left)), 0), W - 1)
    top_i = min(max(int(round(top)), 0), H - 1)
    right_i = min(max(int(round(right)), left_i + 1), W)
    bottom_i = min(max(int(round(bottom)), top_i + 1), H)
    return img.crop((left_i, top_i, right_i, bottom_i))


def _face_box(face: Any) -> tuple[float, float, float, float] | None:
    box = face.get("box") if isinstance(face, dict) else getattr(face, "box", face)
    if not isinstance(box, Sequence) or isinstance(box, (str, bytes)) or len(box) != 4:
        return None
    try:
        fx, fy, fw, fh = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (fx, fy, fw, fh)) or fw <= 0 or fh <= 0:
        return None
    return fx, fy, fw, fh


def crop_aspect(img: Image.Image, ratio: Any, anchor: str = "center", faces: Iterable[Any] | None = None) -> Image.Image:
    """Largest crop with ``ratio`` (w/h), positioned by ``anchor``: center | smart | face.

    ``faces`` are objects or dicts with ``box = (x, y, w, h)``; the largest one is centred.
    Without usable faces the ``face`` anchor falls back to ``smart``.
    """
    value = parse_ratio(ratio)
    if anchor not in ("center", "smart", "face"):
        raise ValueError(f"Invalid anchor {anchor!r} (use 'center', 'smart' or 'face')")
    W, H = img.size
    if W / H > value:
        h, w = H, min(W, max(1, int(round(H * value))))
    else:
        w, h = W, min(H, max(1, int(round(W / value))))
    if (w, h) == (W, H):
        return img.copy()
    x, y = (W - w) // 2, (H - h) // 2
    if anchor == "face":
        boxes = [b for b in (_face_box(f) for f in (faces or [])) if b is not None]
        if boxes:
            fx, fy, fw, fh = max(boxes, key=lambda b: b[2] * b[3])
            x = int(round(fx + fw / 2 - w / 2))
            y = int(round(fy + fh / 2 - h / 2))
            x, y = min(max(x, 0), W - w), min(max(y, 0), H - h)
        else:
            anchor = "smart"
    if anchor == "smart":
        x, y = smart_box(img, w, h)
    return img.crop((x, y, x + w, y + h))


def _corner_background(samples: list[tuple[int, ...]], tolerance: int, has_alpha: bool) -> tuple[int, ...]:
    """Pick the corner colour shared by most corners (transparent corners match each other)."""
    def same(a: tuple[int, ...], b: tuple[int, ...]) -> bool:
        if has_alpha and a[-1] == 0 and b[-1] == 0:
            return True
        return max(abs(p - q) for p, q in zip(a, b)) <= tolerance

    counts = [sum(same(a, b) for b in samples) for a in samples]
    return samples[counts.index(max(counts))]


def trim(img: Image.Image, tolerance: int = 12, padding: int = 0) -> Image.Image:
    """Auto-crop uniform borders whose colour is sampled from the corners (alpha = 0 is border too)."""
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or not 0 <= tolerance <= 255:
        raise ValueError("tolerance must be between 0 and 255")
    if isinstance(padding, bool) or not isinstance(padding, (int, float)) or padding < 0:
        raise ValueError("padding must be 0 or more pixels")
    tolerance, padding = int(tolerance), int(padding)
    if img.mode not in ("RGB", "RGBA", "L", "LA"):
        img = img.convert("RGBA" if "A" in img.getbands() or "transparency" in img.info else "RGB")
    W, H = img.size
    has_alpha = img.mode in ("RGBA", "LA")
    bands = img.split()
    colour_bands = bands[:-1] if has_alpha else bands
    alpha = bands[-1] if has_alpha else None

    samples = []
    for corner in ((0, 0), (W - 1, 0), (0, H - 1), (W - 1, H - 1)):
        px = img.getpixel(corner)
        samples.append(tuple(px) if isinstance(px, tuple) else (px,))
    bg = _corner_background(samples, tolerance, has_alpha)

    opaque_lut = [0] + [255] * 255
    if alpha is not None and bg[-1] == 0:
        content = alpha.point(opaque_lut)
    else:
        content = None
        for band, ref in zip(colour_bands, bg):
            lut = [255 if abs(v - ref) > tolerance else 0 for v in range(256)]
            mask = band.point(lut)
            content = mask if content is None else ImageChops.lighter(content, mask)
        if alpha is not None:
            content = ImageChops.darker(content, alpha.point(opaque_lut))
    bbox = content.getbbox()
    if bbox is None:  # uniform image: nothing sensible to trim
        return img.copy()
    left, top, right, bottom = bbox
    box = (max(0, left - padding), max(0, top - padding), min(W, right + padding), min(H, bottom + padding))
    if box == (0, 0, W, H):
        return img.copy()
    return img.crop(box)


# ------------------------------------------------------------------ resize
def _length(value: Any, name: str, unit: str, dpi: float) -> int:
    number = _positive(value, name)
    return max(1, to_px(number, unit, dpi))


def resize(
    img: Image.Image,
    mode: str,
    *,
    width: float | None = None,
    height: float | None = None,
    scale: float | None = None,
    size: float | None = None,
    megapixels: float | None = None,
    unit: str = "px",
    dpi: float = 300,
    upscale: bool = True,
    resample: str = "lanczos",
    background: Any = "#ffffff",
) -> Image.Image:
    """Resize per PLAN.md §3 "Resize modes"; results are whole pixels ≥ 1.

    ``unit`` ≠ px converts width/height/size with ``dpi`` (the caller records the pipeline dpi).
    ``upscale=False`` never enlarges the picture: fit/scale/edge/megapixel modes keep the original
    size, ``fill`` crops the target aspect at native resolution, ``pad`` pads without enlarging the
    content, ``exact`` clamps each axis to its original length.
    """
    if mode not in RESIZE_MODES:
        raise ValueError(f"Invalid resize mode {mode!r} (use one of {', '.join(RESIZE_MODES)})")
    if unit not in UNITS:
        raise ValueError(f"Invalid unit {unit!r} (use one of {', '.join(UNITS)})")
    if resample not in RESAMPLE:
        raise ValueError(f"Invalid resample filter {resample!r} (use one of {', '.join(RESAMPLE)})")
    if unit != "px":
        dpi = _check_dpi(dpi)
    filt = RESAMPLE[resample]
    W, H = img.size

    def need_both() -> tuple[int, int]:
        if width is None or height is None:
            raise ValueError(f"Resize mode '{mode}' needs both width and height")
        return _length(width, "width", unit, dpi), _length(height, "height", unit, dpi)

    def finish(out_w: int, out_h: int, box: tuple[float, float, float, float] | None = None) -> Image.Image:
        out_w, out_h = max(1, int(out_w)), max(1, int(out_h))
        _check_output_size(out_w, out_h)
        if box is None and (out_w, out_h) == (W, H):
            return img.copy()
        gap = None if filt == Image.Resampling.NEAREST else _REDUCING_GAP
        return img.resize((out_w, out_h), filt, box=box, reducing_gap=gap)

    def uniform(factor: float) -> Image.Image:
        if not upscale and factor > 1:
            return img.copy()
        return finish(max(1, round(W * factor)), max(1, round(H * factor)))

    if mode == "fit":
        if width is None and height is None:
            raise ValueError("Resize mode 'fit' needs a width, a height or both")
        tw = _length(width, "width", unit, dpi) if width is not None else None
        th = _length(height, "height", unit, dpi) if height is not None else None
        if tw is not None and (th is None or tw / W <= th / H):
            out = (tw, max(1, round(H * tw / W)))
            factor = tw / W
        else:
            assert th is not None
            out = (max(1, round(W * th / H)), th)
            factor = th / H
        if not upscale and factor > 1:
            return img.copy()
        return finish(*out)

    if mode == "fill":
        tw, th = need_both()
        target = tw / th
        if W / H > target:  # source is wider: crop the sides
            bw, bh = H * target, float(H)
        else:
            bw, bh = float(W), W / target
        if not upscale and (tw > bw or th > bh):
            # Keep the target aspect at native resolution instead of enlarging.
            k = min(W / tw, H / th, 1.0)
            cw, ch = max(1, min(W, round(tw * k))), max(1, min(H, round(th * k)))
            left, top = (W - cw) // 2, (H - ch) // 2
            _check_output_size(cw, ch)
            return img.crop((left, top, left + cw, top + ch))
        left, top = (W - bw) / 2, (H - bh) / 2
        return finish(tw, th, box=(left, top, left + bw, top + bh))

    if mode == "pad":
        tw, th = need_both()
        factor = min(tw / W, th / H)
        if not upscale:
            factor = min(factor, 1.0)
        cw, ch = min(tw, max(1, round(W * factor))), min(th, max(1, round(H * factor)))
        _check_output_size(tw, th)
        content = img if (cw, ch) == (W, H) else finish(cw, ch)
        base, fill_value = _with_fill(content, background)
        canvas = Image.new(base.mode, (tw, th), fill_value)
        canvas.paste(base, ((tw - cw) // 2, (th - ch) // 2))
        return canvas

    if mode == "exact":
        tw, th = need_both()
        if not upscale:
            tw, th = min(tw, W), min(th, H)
        return finish(tw, th)

    if mode == "scale":
        if scale is None:
            raise ValueError("Resize mode 'scale' needs a scale percentage")
        return uniform(_positive(scale, "scale") / 100.0)

    if mode in ("long_edge", "short_edge"):
        if size is None:
            raise ValueError(f"Resize mode '{mode}' needs a size")
        target = _length(size, "size", unit, dpi)
        edge = max(W, H) if mode == "long_edge" else min(W, H)
        if not upscale and target > edge:
            return img.copy()
        if (W >= H) == (mode == "long_edge"):
            return finish(target, max(1, round(H * target / W)))
        return finish(max(1, round(W * target / H)), target)

    # megapixels
    if megapixels is None:
        raise ValueError("Resize mode 'megapixels' needs a megapixel value")
    factor = math.sqrt(_positive(megapixels, "megapixels") * 1e6 / (W * H))
    return uniform(factor)


def pad(img: Image.Image, top: int = 0, right: int = 0, bottom: int = 0, left: int = 0,
        color: Any = "#ffffff") -> Image.Image:
    """Add a solid border (``color`` may be ``transparent``)."""
    sides = {"top": top, "right": right, "bottom": bottom, "left": left}
    values = {}
    for name, value in sides.items():
        number = _finite(value, name)
        if number < 0 or number != int(number):
            raise ValueError(f"{name} padding must be a whole number of pixels ≥ 0")
        values[name] = int(number)
    W, H = img.size
    out_w, out_h = W + values["left"] + values["right"], H + values["top"] + values["bottom"]
    _check_output_size(out_w, out_h)
    base, fill_value = _with_fill(img, color)
    if (out_w, out_h) == (W, H):
        return base.copy() if base is img else base
    canvas = Image.new(base.mode, (out_w, out_h), fill_value)
    canvas.paste(base, (values["left"], values["top"]))
    return canvas


# ------------------------------------------------------------------ tone
def adjust(img: Image.Image, brightness: float = 1.0, contrast: float = 1.0, saturation: float = 1.0,
           sharpness: float = 1.0, grayscale: bool = False, auto_contrast: bool = False) -> Image.Image:
    """PIL ImageEnhance-style adjustments (1.0 = unchanged); alpha is preserved untouched."""
    factors = {"brightness": brightness, "contrast": contrast, "saturation": saturation, "sharpness": sharpness}
    for name, value in factors.items():
        number = _finite(value, name)
        if not 0 <= number <= 100:
            raise ValueError(f"{name} must be between 0 and 100 (1.0 = unchanged)")
        factors[name] = number
    alpha = img.getchannel("A") if img.mode in ("RGBA", "LA") else None
    gray_source = img.mode in ("L", "LA")
    base = img.convert("L" if gray_source else "RGB") if img.mode not in ("RGB", "L") else img
    if auto_contrast:
        base = ImageOps.autocontrast(base, cutoff=0.5, preserve_tone=True)
    if factors["brightness"] != 1.0:
        base = ImageEnhance.Brightness(base).enhance(factors["brightness"])
    if factors["contrast"] != 1.0:
        base = ImageEnhance.Contrast(base).enhance(factors["contrast"])
    if factors["saturation"] != 1.0 and base.mode == "RGB":
        base = ImageEnhance.Color(base).enhance(factors["saturation"])
    if factors["sharpness"] != 1.0:
        base = ImageEnhance.Sharpness(base).enhance(factors["sharpness"])
    if grayscale and base.mode != "L":
        base = base.convert("L")
    if base is img:  # nothing changed: still return a new object (ops never alias their input)
        return img.copy()
    if alpha is not None:
        base.putalpha(alpha)
    return base


# ------------------------------------------------------------------ saliency
def _robust_norm(values: np.ndarray) -> np.ndarray:
    top = float(np.percentile(values, 99))
    if top <= 1e-6:
        return np.zeros_like(values)
    return np.clip(values / top, 0.0, 1.0)


def smart_box(img: Image.Image, w: int, h: int) -> tuple[int, int]:
    """Best top-left corner for a ``w×h`` window by saliency (edges + colourfulness, mild centre bias)."""
    W, H = img.size
    w = int(min(max(1, round(_finite(w, "w"))), W))
    h = int(min(max(1, round(_finite(h, "h"))), H))
    if (w, h) == (W, H):
        return 0, 0
    s = min(1.0, _SALIENCY_SIDE / max(W, H))
    sw, sh = max(1, round(W * s)), max(1, round(H * s))
    has_alpha = img.mode in ("RGBA", "LA") or "transparency" in img.info
    target_mode = "RGBA" if has_alpha else "RGB"
    small = img if img.mode in ("RGB", "RGBA", "L", "LA") else img.convert(target_mode)
    if (sw, sh) != (W, H):  # downscale first: never convert a full-size copy
        small = small.resize((sw, sh), Image.Resampling.BILINEAR, reducing_gap=2.0)
    arr = np.asarray(small.convert(target_mode), dtype=np.float32) / 255.0
    rgb = arr[..., :3]
    lum = rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    if min(sh, sw) >= 2:
        gy, gx = np.gradient(lum)
        grad = np.hypot(gx, gy)
    else:
        grad = np.zeros_like(lum)
    rg = rgb[..., 0] - rgb[..., 1]
    yb = 0.5 * (rgb[..., 0] + rgb[..., 1]) - rgb[..., 2]
    saliency = 0.6 * _robust_norm(grad) + 0.4 * _robust_norm(np.hypot(rg, yb))
    if has_alpha:
        saliency = saliency * arr[..., 3]

    ww, wh = min(sw, max(1, round(w * s))), min(sh, max(1, round(h * s)))
    integral = np.zeros((sh + 1, sw + 1), dtype=np.float64)
    integral[1:, 1:] = saliency.astype(np.float64).cumsum(0).cumsum(1)
    ny, nx = sh - wh + 1, sw - ww + 1
    sums = (integral[wh:wh + ny, ww:ww + nx] - integral[:ny, ww:ww + nx]
            - integral[wh:wh + ny, :nx] + integral[:ny, :nx])
    cx = (np.arange(nx) + ww / 2) / sw - 0.5
    cy = (np.arange(ny) + wh / 2) / sh - 0.5
    dist = np.sqrt(cy[:, None] ** 2 + cx[None, :] ** 2) / math.sqrt(0.5)
    score = (sums + 1e-9) * (1.0 - 0.15 * dist)
    by, bx = np.unravel_index(int(np.argmax(score)), score.shape)
    x = int(round((bx + ww / 2) / s - w / 2))
    y = int(round((by + wh / 2) / s - h / 2))
    return min(max(x, 0), W - w), min(max(y, 0), H - h)
