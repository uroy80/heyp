"""Classic top/bottom-caption memes, captions inside the picture or on bands outside it.

Anton (OFL) stands in for the proprietary Impact. ``size`` is "auto" or the font size as a fraction
of the image height. Auto follows the UI's ``memeLayout`` (static/js/stagekit.js): the largest size
≤ 14 % of the height at which every line of the caption fits 94 % of the width and the block 30 % of
the height; when that would be tiny (a long caption) the caption wraps onto up to three lines instead.
A caption is never laid out wider than that or taller than 30 % of the height: one that can't fit at
the 3.5 % floor is set smaller (with a warning in ``Prepared.warnings``). The outline is
``outline_width`` × the font size (``outline`` "none" = no outline). Captions sit 2.5 % of the height
from the edges. Blank canvases come from ``canvas()`` — we ship no meme template images.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from PIL import Image

from . import ops, text

__all__ = ["apply", "prepare", "stamp", "Prepared", "canvas", "CANVAS_PRESETS"]

CANVAS_PRESETS = {
    "square": (1080, 1080), "landscape": (1200, 675), "portrait": (1080, 1350), "story": (1080, 1920),
    "wide": (1500, 500),
}
_WIDTH_SHARE = 0.94     # a caption line may use this share of the width
_EDGE = 0.025           # distance from the top/bottom edge, fraction of the height
_MAX_SIZE = 0.14        # auto size cap, fraction of the height
_MIN_SIZE = 0.035       # auto size floor, fraction of the height
_WRAP_BELOW = 0.06      # auto: wrap long captions rather than going below this size
_MAX_CAPTION_SHARE = 0.3  # one caption may use up to this share of the image height (wrapped)
_MAX_LINES = 3
_LINE_SPACING = 1.08
DEFAULT_OUTLINE_WIDTH = 0.07


def _get(op: Any, name: str, default: Any = None) -> Any:
    if isinstance(op, dict):
        return op.get(name, default)
    return getattr(op, name, default)


def canvas(preset: str = "square", color: Any = "#ffffff") -> Image.Image:
    """A blank canvas for a meme or a design (RGB, or RGBA when ``color`` is translucent)."""
    size = CANVAS_PRESETS.get(preset)
    if size is None:
        raise ValueError(f"Unknown canvas {preset!r} (choose from {', '.join(CANVAS_PRESETS)})")
    rgba = ops.parse_color(color)
    return Image.new("RGBA", size, rgba) if rgba[3] < 255 else Image.new("RGB", size, rgba[:3])


def _no_outline(outline: Any) -> bool:
    return outline is None or (isinstance(outline, str) and outline.strip().lower() in ("none", "transparent", "")) \
        or ops.parse_color(outline)[3] == 0


def _caption_size(caption: str, font: str | None, width: float, explicit: float | None, H: int,
                  stroke_ratio: float) -> tuple[int, float | None, bool]:
    """(font px, wrap width or None, set below the auto floor) for one caption.

    Auto: one line per explicit line at the largest size whose widest line fits ``width`` and whose block
    fits _MAX_CAPTION_SHARE of the height; a long caption that would drop below _WRAP_BELOW wraps instead
    (≤ _MAX_LINES lines while the size stays above the floor). Nothing is ever laid out wider than ``width``
    or taller than the share: when even the floor is too big the caption shrinks below it (img-03).
    """
    if explicit is not None:
        return max(text.MIN_FONT_PX, int(round(explicit * H))), width, False
    hi, lo = max(text.MIN_FONT_PX, int(H * _MAX_SIZE)), max(text.MIN_FONT_PX, int(round(H * _MIN_SIZE)))
    max_h = H * _MAX_CAPTION_SHARE
    ref = 100
    ref_w, ref_h = text.measure(caption, font, ref, line_spacing=_LINE_SPACING)
    fit_w = ref * width / ref_w / (1 + 2 * stroke_ratio) if ref_w else hi
    fit_h = ref * max_h / (ref_h + 2 * stroke_ratio * ref) if ref_h else hi
    one = int(min(hi, fit_w, fit_h))   # every explicit line on one line (what the UI previews)

    def fits(size: int) -> bool:   # small sizes round metrics up, so check the scaled estimate for real
        w, h = text.measure(caption, font, size, line_spacing=_LINE_SPACING)
        stroke = 2 * max(1, round(size * stroke_ratio)) if stroke_ratio else 0
        return w + stroke <= width + 1 and h + stroke <= max_h + 1

    while one > text.MIN_FONT_PX and not fits(one):
        one = max(text.MIN_FONT_PX, min(one - 1, int(one * 0.96)))
    if len(caption) < 12 or (one >= H * _WRAP_BELOW and one >= lo):
        return max(text.MIN_FONT_PX, one), None, one < lo
    wrapped = text.fit_size(caption, font, width, max_h, max_size=hi, min_size=text.MIN_FONT_PX,
                            stroke_ratio=stroke_ratio, line_spacing=_LINE_SPACING)
    while wrapped > lo and len(text.wrap(caption, font, wrapped, width - 2 * round(wrapped * stroke_ratio))) > _MAX_LINES:
        wrapped = max(lo, int(wrapped * 0.92))
    if one >= lo and one >= wrapped:
        return one, None, False
    return wrapped, width, wrapped < lo


def _caption_layer(caption: str, op: Any, size: int, wrap_width: float | None, color: Any, outline: Any,
                   stroke_ratio: float) -> Image.Image:
    stroke = 0 if _no_outline(outline) else max(1, int(round(size * stroke_ratio)))
    return text.render(caption, font=_get(op, "font", "anton"), size=size, color=color, align="center",
                       max_width=(wrap_width - 2 * stroke) if wrap_width else None, line_spacing=_LINE_SPACING,
                       stroke_color=outline if stroke else None, stroke_width=stroke)


@dataclass
class Prepared:
    """Rendered captions for one canvas size (animations stamp them on every frame)."""

    top: Image.Image | None
    bottom: Image.Image | None
    pad: int
    layout: str
    band_color: tuple[int, int, int, int]
    warnings: list[str] = field(default_factory=list)

    def output_size(self, size: tuple[int, int]) -> tuple[int, int]:
        if self.layout != "outside":
            return size
        return size[0], size[1] + self._band(self.top) + self._band(self.bottom)

    def _band(self, layer: Image.Image | None) -> int:
        return layer.height + 2 * self.pad if layer is not None else 0


def prepare(op: Any, size: tuple[int, int]) -> Prepared:
    """Validate ``op`` (a MemeOp or dict) and render its captions for a ``size`` canvas."""
    top = str(_get(op, "top", "") or "")
    bottom = str(_get(op, "bottom", "") or "")
    if _get(op, "uppercase", True):
        top, bottom = top.upper(), bottom.upper()
    if not top.strip() and not bottom.strip():
        raise ValueError("Enter a top or a bottom caption")
    layout = _get(op, "layout", "inside") or "inside"
    if layout not in ("inside", "outside"):
        raise ValueError("layout must be 'inside' or 'outside'")
    font = _get(op, "font", "anton")
    text.resolve(font)
    color = _get(op, "color") or ("#ffffff" if layout == "inside" else "#000000")
    outline = _get(op, "outline")
    if outline is None:
        outline = "#000000" if layout == "inside" else "none"
    raw_ratio = _get(op, "outline_width", DEFAULT_OUTLINE_WIDTH)
    stroke_ratio = 0.0 if _no_outline(outline) else float(DEFAULT_OUTLINE_WIDTH if raw_ratio is None else raw_ratio)
    size_opt = _get(op, "size", "auto")
    explicit = None if size_opt in (None, "auto") else float(size_opt)
    W, H = size
    width = W * _WIDTH_SHARE
    captions = []
    warnings = []
    for name, caption in (("top", top), ("bottom", bottom)):
        if caption.strip():
            px, wrap_width, small = _caption_size(caption, font, width, explicit, H, stroke_ratio)
            captions.append(_caption_layer(caption, op, px, wrap_width, color, outline, stroke_ratio))
            if small:
                warnings.append(f"The {name} caption is long for this picture, so it was set in small type; "
                                "shorten it to make it bigger")
        else:
            captions.append(None)
    band = ops.parse_color(_get(op, "band_color", "#ffffff") or "#ffffff")
    return Prepared(captions[0], captions[1], int(round(H * _EDGE)), layout, band, warnings)


def stamp(img: Image.Image, prepared: Prepared) -> Image.Image:
    """Draw prepared captions onto ``img`` (``outside`` adds bands above/below)."""
    W, H = img.size
    had_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    top_layer, bottom_layer, pad = prepared.top, prepared.bottom, prepared.pad
    if prepared.layout == "outside":
        top_h = prepared._band(top_layer)
        bottom_h = prepared._band(bottom_layer)
        out = ops.pad(img, top=top_h, bottom=bottom_h, color=prepared.band_color).convert("RGBA")
        if top_layer is not None:
            _paste_clipped(out, top_layer, (W - top_layer.width) // 2, pad)
        if bottom_layer is not None:
            _paste_clipped(out, bottom_layer, (W - bottom_layer.width) // 2, top_h + H + pad)
        return out if had_alpha or prepared.band_color[3] < 255 else out.convert("RGB")
    base = img.convert("RGBA")
    if top_layer is not None:
        _paste_clipped(base, top_layer, (W - top_layer.width) // 2, pad)
    if bottom_layer is not None:
        _paste_clipped(base, bottom_layer, (W - bottom_layer.width) // 2, H - pad - bottom_layer.height)
    return base if had_alpha else base.convert("RGB")


def apply(img: Image.Image, op: Any) -> Image.Image:
    """Add the captions of ``op`` (a MemeOp or dict) to ``img``."""
    return stamp(img, prepare(op, img.size))


def _paste_clipped(base: Image.Image, layer: Image.Image, left: int, top: int) -> None:
    W, H = base.size
    x0, y0 = max(0, left), max(0, top)
    x1, y1 = min(W, left + layer.width), min(H, top + layer.height)
    if x1 > x0 and y1 > y0:
        base.alpha_composite(layer.crop((x0 - left, y0 - top, x1 - left, y1 - top)), (x0, y0))
