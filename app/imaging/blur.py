"""Blur / pixelate / black-out regions, and suggested regions from face + plate detectors.

Regions are fractions of the image (x, y, w, h in [0, 1]) so the same boxes work on a preview, the
full-size image and every frame of an animation. ``strength`` scales the effect with the
region size, so a small far-away plate and a large face are both unreadable at the same setting
(``strength`` s is 0–1, default 0.6):

* blur — Gaussian, sigma = (0.05 + 0.20·s) × the region's long side (default ≈ side/6;
  research: σ ≥ box/8 defeats plate reading). The blur samples real context around the box, so the
  edge blends instead of showing a dark frame.
* pixelate — blocks of (0.04 + 0.16·s) × long side (default ≈ 7 blocks across), anchored at the box.
* solid — filled with ``color`` (the only irreversible-by-construction mode; recommended for text).

Transparent images are processed premultiplied, so no dark fringes appear at alpha edges.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFilter

MODES = ("blur", "pixelate", "solid")
SHAPES = ("rect", "ellipse")
MAX_REGIONS = 500
_SUPERSAMPLE = 4  # ellipse masks are drawn 4× larger, then reduced, for smooth edges


@dataclass(frozen=True)
class Region:
    """A box in fractions of the image size."""

    x: float
    y: float
    w: float
    h: float
    shape: str = "rect"

    def to_pixels(self, width: int, height: int) -> tuple[int, int, int, int] | None:
        """(x0, y0, x1, y1) clamped to the image, rounded outwards; None when empty."""
        x0 = max(0, math.floor(self.x * width + 1e-6))
        y0 = max(0, math.floor(self.y * height + 1e-6))
        x1 = min(width, math.ceil((self.x + self.w) * width - 1e-6))
        y1 = min(height, math.ceil((self.y + self.h) * height - 1e-6))
        if x1 - x0 < 1 or y1 - y0 < 1:
            return None
        return x0, y0, x1, y1


def _region(value: object) -> Region:
    if isinstance(value, Region):
        return value
    if isinstance(value, dict):
        return Region(float(value["x"]), float(value["y"]), float(value["w"]), float(value["h"]),
                      str(value.get("shape", "rect")))
    get = lambda k, d=None: getattr(value, k, d)  # noqa: E731 — pydantic model
    return Region(float(get("x")), float(get("y")), float(get("w")), float(get("h")), str(get("shape", "rect")))


def blur_sigma(side: int, strength: float) -> float:
    """Gaussian sigma (px) for a region whose long side is ``side`` px; strength in [0, 1]."""
    return max(1.5, (0.05 + 0.20 * strength) * side)


def pixel_block(side: int, strength: float) -> int:
    """Pixelation block size (px) for a region whose long side is ``side`` px; strength in [0, 1]."""
    return max(2, round((0.04 + 0.16 * strength) * side))


def _ellipse_mask(w: int, h: int) -> Image.Image:
    s = _SUPERSAMPLE if w * h <= 4_000_000 else 1  # huge regions: aliasing is invisible, memory isn't
    big = Image.new("L", (w * s, h * s), 0)
    ImageDraw.Draw(big).ellipse((0, 0, w * s - 1, h * s - 1), fill=255)
    return big.resize((w, h), Image.Resampling.BOX) if s > 1 else big


def _work_mode(img: Image.Image) -> tuple[Image.Image, str]:
    """Image in a mode the filters handle (premultiplied when it has alpha) and the mode to return."""
    mode = img.mode
    if mode in ("RGB", "L"):
        return img.copy(), mode
    if mode == "RGBA":
        return img.convert("RGBa"), mode
    if mode == "LA":
        return img.convert("La"), mode
    alpha = "A" in img.getbands() or "transparency" in img.info
    if alpha:
        return img.convert("RGBA").convert("RGBa"), "RGBA"
    return img.convert("RGB"), "RGB"


def _fill(mode: str, color: tuple[int, int, int, int]) -> tuple[int, ...] | int:
    r, g, b, a = color
    if mode == "RGB":
        return (r, g, b)
    if mode == "L":
        return round(0.299 * r + 0.587 * g + 0.114 * b)
    if mode == "RGBa":  # premultiplied
        return (r * a // 255, g * a // 255, b * a // 255, a)
    if mode == "La":
        lum = round(0.299 * r + 0.587 * g + 0.114 * b)
        return (lum * a // 255, a)
    return color


def blur_regions(img: Image.Image, regions: Iterable[object], *, mode: str = "blur", strength: float = 0.6,
                 color: tuple[int, int, int, int] = (0, 0, 0, 255),
                 check_cancel: Callable[[], None] | None = None) -> Image.Image:
    """Return a copy of ``img`` with every region blurred / pixelated / filled.

    ``regions``: Region objects, dicts or models with x, y, w, h (fractions) and optional shape.
    """
    if mode not in MODES:
        raise ValueError(f"blur mode must be one of {', '.join(MODES)}")
    if not 0.0 <= float(strength) <= 1.0:
        raise ValueError("strength must be within 0..1")
    boxes = [_region(r) for r in regions]
    if len(boxes) > MAX_REGIONS:
        raise ValueError(f"at most {MAX_REGIONS} regions")
    for r in boxes:
        if r.shape not in SHAPES:
            raise ValueError(f"region shape must be one of {', '.join(SHAPES)}")
    work, out_mode = _work_mode(img)
    width, height = work.size
    for r in boxes:
        if check_cancel is not None:
            check_cancel()
        px = r.to_pixels(width, height)
        if px is None:
            continue
        x0, y0, x1, y1 = px
        w, h = x1 - x0, y1 - y0
        side = max(w, h)
        if mode == "solid":
            patch = Image.new(work.mode, (w, h), _fill(work.mode, color))
        elif mode == "pixelate":
            block = pixel_block(side, strength)
            small = work.crop(px).resize((max(1, math.ceil(w / block)), max(1, math.ceil(h / block))),
                                         Image.Resampling.BOX)
            patch = small.resize((small.width * block, small.height * block), Image.Resampling.NEAREST)
            patch = patch.crop((0, 0, w, h))
        else:
            sigma = blur_sigma(side, strength)
            margin = math.ceil(2.5 * sigma)
            cx0, cy0 = max(0, x0 - margin), max(0, y0 - margin)
            cx1, cy1 = min(width, x1 + margin), min(height, y1 + margin)
            region = work.crop((cx0, cy0, cx1, cy1)).filter(ImageFilter.GaussianBlur(sigma))
            patch = region.crop((x0 - cx0, y0 - cy0, x0 - cx0 + w, y0 - cy0 + h))
        mask = _ellipse_mask(w, h) if r.shape == "ellipse" else None
        work.paste(patch, (x0, y0), mask)
    if work.mode != out_mode:
        work = work.convert(out_mode)
    return work


# ------------------------------------------------------------------ suggestions (faces + plates)
# How much to grow a detector box so the whole face / plate is covered (fractions of the box).
FACE_GROW = (0.15, 0.30, 0.15, 0.12)   # left, top (hair/forehead), right, bottom (chin)
PLATE_GROW = (0.10, 0.10, 0.10, 0.10)
LEVELS = {
    # level: (face min score, plate min score shown, plate score preselected, tile pass)
    "low": (0.80, 0.40, 0.40, False),
    "recommended": (0.60, 0.15, 0.25, True),
    "high": (0.45, 0.10, 0.20, True),
}
FACE_SELECT = 0.70  # faces at or above this score are preselected
FACE_TILE = 1280
ANALYSIS_MAX_SIDE = 5120


@dataclass(frozen=True)
class Suggestion:
    kind: str     # "face" | "plate"
    x: float      # fractions, already grown and clamped
    y: float
    w: float
    h: float
    score: float
    selected: bool
    shape: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "x": round(float(self.x), 6), "y": round(float(self.y), 6),
                "w": round(float(self.w), 6), "h": round(float(self.h), 6), "shape": self.shape,
                "score": round(float(self.score), 4), "selected": bool(self.selected)}


def _grow(x: float, y: float, w: float, h: float, grow: Sequence[float], width: int, height: int
          ) -> tuple[float, float, float, float]:
    gl, gt, gr, gb = grow
    x0, y0 = max(0.0, x - gl * w), max(0.0, y - gt * h)
    x1, y1 = min(float(width), x + w + gr * w), min(float(height), y + h + gb * h)
    return x0 / width, y0 / height, (x1 - x0) / width, (y1 - y0) / height


def _detect_faces(img: Image.Image, min_score: float, tiles: bool,
                  check_cancel: Callable[[], None] | None) -> list[tuple[float, float, float, float, float]]:
    """Faces as (x, y, w, h, score) in image px: one ≤1024-px pass, then 1280-px tiles of a ≤5120-px copy
    so small faces in group photos are found too."""
    from app.imaging import face, plates

    width, height = img.size
    found = [(*(f.raw_box or f.box), f.score) for f in face.detect(img, min_score=min_score)]
    if tiles and max(width, height) > FACE_TILE * 1.25:
        k = min(1.0, ANALYSIS_MAX_SIDE / max(width, height))
        work = img if k == 1.0 else img.resize((round(width * k), round(height * k)), Image.Resampling.BILINEAR,
                                               reducing_gap=2.0)
        for oy in plates.tile_origins(work.height, FACE_TILE):
            for ox in plates.tile_origins(work.width, FACE_TILE):
                if check_cancel is not None:
                    check_cancel()
                crop = work.crop((ox, oy, min(work.width, ox + FACE_TILE), min(work.height, oy + FACE_TILE)))
                for f in face.detect(crop, min_score=min_score, max_side=FACE_TILE):
                    fx, fy, fw, fh = f.raw_box or f.box
                    found.append(((fx + ox) / k, (fy + oy) / k, fw / k, fh / k, f.score))
    dets = [plates.Detection(x, y, w, h, s, "face") for x, y, w, h, s in found if w > 0 and h > 0]
    return [(d.x, d.y, d.w, d.h, d.score) for d in plates.merge(dets, threshold=0.5)]


def suggest_regions(img: Image.Image, what: Iterable[str] = ("faces", "plates"), *, level: str = "recommended",
                    check_cancel: Callable[[], None] | None = None) -> tuple[list[Suggestion], list[str]]:
    """Suggested blur regions (fractions, grown to cover the whole face / plate) and warnings.

    Raises ModelUnavailable (a RuntimeError) when none of the requested detectors is available.
    """
    from app.imaging import face, plates
    from app.imaging.ortsession import ModelUnavailable

    if level not in LEVELS:
        raise ValueError(f"level must be one of {', '.join(LEVELS)}")
    wanted = list(dict.fromkeys(what))
    face_min, plate_min, plate_select, tiles = LEVELS[level]
    width, height = img.size
    out: list[Suggestion] = []
    warnings: list[str] = []
    ran = 0
    if "faces" in wanted:
        if face.available():
            ran += 1
            for x, y, w, h, s in _detect_faces(img, face_min, tiles, check_cancel):
                fx, fy, fw, fh = _grow(x, y, w, h, FACE_GROW, width, height)
                out.append(Suggestion("face", fx, fy, fw, fh, s, s >= FACE_SELECT, "ellipse"))
        else:
            warnings.append("Face detection is unavailable; mark faces by hand")
    if "plates" in wanted:
        if plates.available():
            ran += 1
            for d in plates.detect(img, min_score=plate_min, tiles=tiles, check_cancel=check_cancel):
                fx, fy, fw, fh = _grow(d.x, d.y, d.w, d.h, PLATE_GROW, width, height)
                out.append(Suggestion("plate", fx, fy, fw, fh, d.score, d.score >= plate_select, "rect"))
        else:
            warnings.append("Licence-plate detection is unavailable; mark plates by hand")
    if wanted and ran == 0:
        raise ModelUnavailable("; ".join(warnings) or "no detector available")
    out.sort(key=lambda s: (not s.selected, -s.score))
    return out, warnings


__all__ = ["Region", "Suggestion", "blur_regions", "suggest_regions", "blur_sigma", "pixel_block",
           "MODES", "SHAPES", "MAX_REGIONS", "LEVELS"]
