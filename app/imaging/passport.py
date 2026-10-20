"""Passport / visa / ID photos: face-aware framing to official head-size rules, and print sheets."""
from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image, ImageDraw

from . import face as face_mod
from . import segment
from .face import Face
from .presets import MM_PER_INCH, PAPERS, PassportPreset, get_preset

ANALYSIS_MASK_MAX = 1600       # long side of the stored person mask (U²-Net works at 320 px anyway)
CROWN_COVERAGE = 0.15          # §5.2: mask must cover ≥15 % of the face-box column span
DEFAULT_HEAD_RATIO = (0.5, 0.69)
MIN_TOP_MARGIN = 0.04          # fraction of height kept above the crown when the eye range allows it
SKULL_RATIO = 1.03             # adult vertex→pupil / pupil→menton (anthropometric norms ≈ 1.0–1.07)
SKULL_SD = 0.06                # its spread between people, as a fraction of pupil→chin
CHIN_FROM_MOUTH = 0.85         # chin below the mouth, in eye→mouth distances (chin cut off in the source)
LEVEL_MIN_DEG = 1.0            # auto-level head roll from this angle …
LEVEL_MAX_DEG = 15.0           # … up to this one; beyond it the photo is left alone and a warning is given
TILT_WARN_DEG = 8.0            # roll that is warned about when levelling is off (ISO/ICAO tolerance ≈ 8°)
COARSE_TILT_DEG = 5.0          # YuNet's eye landmarks show only ~⅓ of a roll: this much means a clear tilt
EDGE_TOUCH = 0.02              # person-mask share on a source border that means "the person is cut there"
SIDE_TOUCH_ROWS = 0.10         # share of head rows touching a frame side that means "hair is cut"
REFINE_FACES = 3               # small faces re-detected on a crop (largest first)
CUT_LINE_COLOR = (128, 128, 128)
MAX_COPIES = 200               # per sheet, also when copies=0 ("as many as fit")
MIN_PHOTO_MM = 5.0             # smaller photos are not printable IDs (and would mean millions of tiles)
SHEET_DPI_DEFAULT = 300
SHEET_DPI_AUTO = (300, 600)    # sheet(dpi=None) follows the source dpi within this print range
SIDES = ("top", "bottom", "left", "right")
_LANCZOS = Image.Resampling.LANCZOS
_BILINEAR = Image.Resampling.BILINEAR
_BICUBIC = Image.Resampling.BICUBIC

Eyes = tuple[tuple[float, float], tuple[float, float]]


@dataclass
class Analysis:
    """Faces (largest first), refined pupils per face and the person mask of one image."""

    faces: list[Face]
    mask: np.ndarray | None = None        # float32 [0, 1], image aspect, long side ≤ ANALYSIS_MASK_MAX
    size: tuple[int, int] | None = None   # (W, H) of the analysed image
    eyes: list[Eyes | None] | None = None  # face.refine_eyes per face; None = not measured yet


def analyze(img: Image.Image) -> Analysis:
    """Detect faces (small ones re-detected on a crop), refine their pupils and segment the person.

    Pure function of the image; callers memoise it (the pipeline keys its LRU on the upload id and
    the preceding ops, so a flipped or edited image never reuses a stale analysis).
    """
    faces = face_mod.detect(img) if face_mod.available() else []
    if faces:
        faces = face_mod.refine_small(img, faces, limit=REFINE_FACES)
    eyes = [face_mod.refine_eyes(img, f) for f in faces]
    mask = None
    if segment.available():
        try:
            mask = _analysis_mask(img, faces[0] if faces else None)
        except RuntimeError:  # model failed to load: degrade to no segmentation
            mask = None
    return Analysis(faces=faces, mask=mask, size=img.size, eyes=eyes)


def _analysis_mask(img: Image.Image, main_face: Face | None) -> np.ndarray:
    """Whole-image person mask, refined by a second pass on the head-and-shoulders region."""
    w, h = img.size
    f = min(1.0, ANALYSIS_MASK_MAX / max(w, h))
    mw, mh = max(1, round(w * f)), max(1, round(h * f))
    mask = segment.person_mask(img, size=(mw, mh))
    if main_face is None:
        return mask

    # U²-Net sees the whole frame at 320 px, so hair edges are coarse when the person is small.
    # A second pass on a square around the head (≈ the passport frame) gives a sharper edge there.
    bx, by, bw, bh = main_face.box
    side = 3.2 * bh
    cx = bx + bw / 2.0
    x0, y0 = max(0, round(cx - side / 2)), max(0, round(by - 0.9 * bh))
    x1, y1 = min(w, round(cx + side / 2)), min(h, round(by - 0.9 * bh + side))
    if (x1 - x0) * (y1 - y0) >= 0.5 * w * h or x1 - x0 < 32 or y1 - y0 < 32:
        return mask
    mx0, my0 = round(x0 * mw / w), round(y0 * mh / h)
    mx1, my1 = max(mx0 + 1, round(x1 * mw / w)), max(my0 + 1, round(y1 * mh / h))
    local = segment.person_mask(img.crop((x0, y0, x1, y1)), size=(mx1 - mx0, my1 - my0))

    # Blend weight ramps up from region edges that lie inside the image (no visible seam).
    rw, rh = mx1 - mx0, my1 - my0
    ramp = max(2.0, 0.08 * max(rw, rh))
    wx = np.ones(rw, np.float32)
    wy = np.ones(rh, np.float32)
    idx_x, idx_y = np.arange(rw, dtype=np.float32), np.arange(rh, dtype=np.float32)
    if mx0 > 0:
        wx = np.minimum(wx, (idx_x + 0.5) / ramp)
    if mx1 < mw:
        wx = np.minimum(wx, (rw - idx_x - 0.5) / ramp)
    if my0 > 0:
        wy = np.minimum(wy, (idx_y + 0.5) / ramp)
    if my1 < mh:
        wy = np.minimum(wy, (rh - idx_y - 0.5) / ramp)
    weight = np.clip(np.outer(wy, wx), 0.0, 1.0)
    region = mask[my0:my1, mx0:mx1]
    mask[my0:my1, mx0:mx1] = weight * local + (1.0 - weight) * region
    return mask


@dataclass(frozen=True)
class _Level:
    """Rotation by −`deg` about `pivot`: source → levelled coordinates (identity when deg is 0)."""

    deg: float = 0.0
    pivot: tuple[float, float] = (0.0, 0.0)

    def _cs(self) -> tuple[float, float]:
        rad = math.radians(self.deg)
        return math.cos(rad), math.sin(rad)

    def to_level(self, x, y):
        if not self.deg:
            return x, y
        c, s = self._cs()
        px, py = self.pivot
        return px + c * (x - px) + s * (y - py), py - s * (x - px) + c * (y - py)

    def to_source(self, x, y):
        if not self.deg:
            return x, y
        c, s = self._cs()
        px, py = self.pivot
        return px + c * (x - px) - s * (y - py), py + s * (x - px) + c * (y - py)


def head_geometry(face: Face, mask: np.ndarray | None = None, *,
                  image_size: tuple[int, int] | None = None, roll: float = 0.0,
                  crown_includes_hair: bool = True) -> dict:
    """Crown/chin/eye-line/centre of a face (§5.2), in image px levelled by `roll` degrees.

    `mask` is a person mask in image coordinates, or at another resolution of the same image
    when `image_size=(W, H)` is given. With `roll` the coordinates are those of the image rotated
    by −roll about the eye midpoint (which keeps its position), i.e. a levelled head.

    - chin: bottom of the unclamped detector box (−0.3…−1.7 % of head height on real photos);
      when the box is cut by the bottom of the image, extrapolated from the mouth instead.
    - hair_y: top of the hair from the mask (searched up from the eye line), clamped to a
      plausible head height; when the mask runs into the top of the image (crown cut off) the
      geometric estimate is used if it is higher.
    - skull_y: top of the skull, for authorities that exclude hair (see _skull_height): the
      anthropometric estimate eye − SKULL_RATIO·(chin − eye), pulled down towards the visible
      top of the head when that is lower (short hair, bald), never above it.
    - crown_y: hair_y, or skull_y when `crown_includes_hair` is False; head_h = chin − crown.
    """
    x, y, w, h = face.box
    if w <= 0 or h <= 0:
        raise ValueError("face box must have a positive size")
    eyes = face.eye_center()
    ex, ey = (float(eyes[0]), float(eyes[1])) if eyes else (x + w / 2.0, y + 0.38 * h)
    lev = _Level(float(roll), (ex, ey))

    # The box bottom is the lowest point of the rounded chin, which a roll lifts by less than
    # cos(roll) would; 1/√cos is the middle ground (the two differ by < 1 % up to 15°).
    chin_src = face.bottom
    chin_y = ey + (chin_src - ey) / math.cos(math.radians(roll)) ** 0.5
    chin_clipped = image_size is not None and chin_src >= image_size[1] - 1
    mouth = [face.landmarks.get(k) for k in ("mouth_right", "mouth_left")]
    if chin_clipped and eyes and all(mouth):
        mouth_y = sum(lev.to_level(mx, my)[1] for mx, my in mouth) / 2.0
        chin_y = max(chin_y, mouth_y + CHIN_FROM_MOUTH * (mouth_y - ey))

    est_crown = ey - (chin_y - ey) - 0.10 * h
    hair_y, source, crown_clipped = est_crown, "estimate", False
    if mask is not None:
        est_h = chin_y - est_crown
        found, clipped = _crown_from_mask(face, np.asarray(mask), lev, chin_y - 1.6 * est_h, image_size)
        if found is not None:
            # Plausibility clamp: big hair/headwear may add height; a broken mask may not shrink the head.
            hair_y = min(max(found, chin_y - 1.5 * est_h), chin_y - 0.85 * est_h)
            source = "mask"
            if clipped:  # the mask runs into the top of the photo: the real crown is higher still
                hair_y, source, crown_clipped = min(hair_y, est_crown), "clipped", True
    skull_y = ey - _skull_height(chin_y - ey, ey - hair_y)
    crown_y = hair_y if crown_includes_hair else skull_y
    return {"crown_y": crown_y, "chin_y": chin_y, "eye_y": ey, "center_x": ex, "head_h": chin_y - crown_y,
            "crown_source": source, "hair_y": hair_y, "skull_y": skull_y, "roll": float(roll),
            "crown_clipped": crown_clipped, "chin_clipped": bool(chin_clipped)}


def _skull_height(chin_below: float, top_above: float) -> float:
    """Expected height of the skull top above the eye line, given the visible top of the head.

    Prior: skull height ~ N(SKULL_RATIO, SKULL_SD) × eye→chin. The skull cannot be higher than the
    visible top (`top_above`, hair included), so the prior is truncated there and its mean is used:
    voluminous hair leaves the anthropometric estimate as it is, while short hair or a bald head
    pulls it smoothly down to the visible top.
    """
    mu, sd = SKULL_RATIO * chin_below, SKULL_SD * chin_below
    if sd <= 0:
        return min(mu, top_above)
    a = max(-30.0, (top_above - mu) / sd)
    cdf = 0.5 * math.erfc(-a / math.sqrt(2.0))
    pdf = math.exp(-0.5 * a * a) / math.sqrt(2.0 * math.pi)
    return min(top_above, mu - sd * pdf / cdf)


def _crown_from_mask(face: Face, mask: np.ndarray, lev: _Level, ceiling_y: float,
                     image_size: tuple[int, int] | None) -> tuple[float | None, bool]:
    """Topmost levelled row (searching up from the eye line) where the mask covers the face columns.

    Returns (row in image px or None, clipped) — clipped means the search ran into the top of the
    source image while the mask still covered the head. Rows are sampled in levelled coordinates,
    so a tilted head is measured along its own axis.
    """
    if mask.ndim != 2 or mask.size == 0:
        return None, False
    mh, mw = mask.shape
    iw, ih = image_size or (mw, mh)
    sx, sy = mw / iw, mh / ih
    x, y, w, h = face.box
    bcx = lev.to_level(x + w / 2.0, y + h / 2.0)[0]
    c0 = math.floor((bcx - w / 2.0) * sx)
    c1 = max(c0 + 1, math.ceil((bcx + w / 2.0) * sx))
    r_eye = int(lev.pivot[1] * sy)
    r_top = min(r_eye, math.floor(ceiling_y * sy) - 1)
    rows = np.arange(r_eye, r_top - 1, -1, dtype=np.float32)
    cols = np.arange(c0, c1, dtype=np.float32)
    if lev.deg:
        qx, qy = np.meshgrid(cols / sx, rows / sy)
        px, py = lev.to_source(qx, qy)
        mx, my = (px * sx).astype(np.float32), (py * sy).astype(np.float32)
    else:
        mx, my = np.meshgrid(cols, rows)
    valid = (mx >= -0.5) & (mx <= mw - 0.5) & (my >= -0.5) & (my <= mh - 0.5)
    vals = cv2.remap(np.ascontiguousarray(mask, dtype=np.float32), mx, my, cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_REPLICATE)
    ok = ((vals > 0.5) & valid).mean(axis=1) >= CROWN_COVERAGE
    inside = valid.mean(axis=1) >= 0.5
    if not (inside[0] and ok[0]):
        return None, False
    gap_tol = max(2, round(0.04 * h * sy))  # tolerate tiny holes in the mask
    top, misses = 0, 0
    for i in range(len(rows)):
        if not inside[i]:
            return float(rows[top]) / sy, misses == 0
        if ok[i]:
            top, misses = i, 0
        else:
            misses += 1
            if misses > gap_tol:
                break
    return float(rows[top]) / sy, False


def _mid(a: float | None, b: float | None) -> float | None:
    vals = [v for v in (a, b) if v is not None]
    return sum(vals) / len(vals) if vals else None


def _in_range(value: float, lo: float | None, hi: float | None, tol: float = 0.005) -> bool:
    return (lo is None or value >= lo - tol) and (hi is None or value <= hi + tol)


def _resolve_preset(preset: PassportPreset | str) -> PassportPreset:
    if isinstance(preset, PassportPreset):
        return preset
    try:
        return get_preset(str(preset))
    except KeyError:
        raise ValueError(f"unknown passport preset {preset!r}") from None


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _render_frame(img: Image.Image, frame: tuple[float, float, float, float], size: tuple[int, int],
                  fill: tuple[int, int, int, int], mask: np.ndarray | None, lev: _Level = _Level(),
                  ) -> tuple[Image.Image, np.ndarray | None, set[str]]:
    """Resample the `frame` (levelled coordinates, may exceed the image) to `size` in one pass.

    Returns (image, mask in output px or None, the source sides the frame extends beyond).
    """
    if lev.deg:
        return _render_rotated(img, frame, size, fill, mask, lev)
    W, H = img.size
    ow, oh = size
    x0, y0, x1, y1 = _snap_frame(frame, size, img.size)
    sx, sy = ow / (x1 - x0), oh / (y1 - y0)

    ox0 = max(0, math.ceil(-x0 * sx - 1e-6))
    oy0 = max(0, math.ceil(-y0 * sy - 1e-6))
    ox1 = min(ow, math.floor((W - x0) * sx + 1e-6))
    oy1 = min(oh, math.floor((H - y0) * sy + 1e-6))
    sides = {s for s, out in zip(SIDES, (oy0 > 0, oy1 < oh, ox0 > 0, ox1 < ow)) if out}

    canvas = Image.new(img.mode, (ow, oh), fill if img.mode == "RGBA" else fill[:3])
    out_mask = np.zeros((oh, ow), np.float32) if mask is not None else None
    if ox1 > ox0 and oy1 > oy0:
        box = (max(0.0, x0 + ox0 / sx), max(0.0, y0 + oy0 / sy),
               min(float(W), x0 + ox1 / sx), min(float(H), y0 + oy1 / sy))
        part = img.resize((ox1 - ox0, oy1 - oy0), _LANCZOS, box=box, reducing_gap=3.0)
        canvas.paste(part, (ox0, oy0))
        if mask is not None:
            mh, mw = mask.shape
            fx, fy = mw / W, mh / H
            mbox = (box[0] * fx, box[1] * fy, min(float(mw), box[2] * fx), min(float(mh), box[3] * fy))
            m = Image.fromarray(np.ascontiguousarray(mask, dtype=np.float32))
            m = m.resize((ox1 - ox0, oy1 - oy0), _BILINEAR, box=mbox)
            out_mask[oy0:oy1, ox0:ox1] = np.clip(np.asarray(m, dtype=np.float32), 0.0, 1.0)
    return canvas, out_mask, sides


def _snap_frame(frame: tuple[float, float, float, float], size: tuple[int, int],
                image_size: tuple[int, int]) -> tuple[float, float, float, float]:
    """Ignore sub-pixel overshoot of an unrotated frame: nudge it back inside the image when it
    is off by < 1 output px (the frame _render_frame actually samples)."""
    W, H = image_size
    x0, y0, x1, y1 = frame
    sx, sy = size[0] / (x1 - x0), size[1] / (y1 - y0)
    for lo, hi, limit, s, axis in ((x0, x1, W, sx, 0), (y0, y1, H, sy, 1)):
        shift = 0.0
        if lo < 0 and -lo * s < 1.0:
            shift = -lo
        elif hi > limit and (hi - limit) * s < 1.0:
            shift = limit - hi
        if shift:
            if axis == 0:
                x0, x1 = x0 + shift, x1 + shift
            else:
                y0, y1 = y0 + shift, y1 + shift
    return x0, y0, x1, y1


def _source_frame(frame: tuple[float, float, float, float], lev: _Level, size: tuple[int, int],
                  image_size: tuple[int, int]) -> dict:
    """info["source_frame"]: where the rendered output frame sits on the source image (see make())."""
    if not lev.deg:
        frame = _snap_frame(frame, size, image_size)
    x0, y0, x1, y1 = frame
    cx, cy = lev.to_source((x0 + x1) / 2.0, (y0 + y1) / 2.0)
    return {"width": int(image_size[0]), "height": int(image_size[1]),
            "center": [round(float(cx), 2), round(float(cy), 2)],
            "size": [round(float(x1 - x0), 2), round(float(y1 - y0), 2)],
            "angle": float(lev.deg)}


def _frame_corners(frame: tuple[float, float, float, float], lev: _Level) -> np.ndarray:
    """The frame's four corners in source coordinates (4×2)."""
    x0, y0, x1, y1 = frame
    xs, ys = np.array([x0, x1, x0, x1]), np.array([y0, y0, y1, y1])
    px, py = lev.to_source(xs, ys)
    return np.stack([px, py], axis=1)


def _render_rotated(img: Image.Image, frame: tuple[float, float, float, float], size: tuple[int, int],
                    fill: tuple[int, int, int, int], mask: np.ndarray | None, lev: _Level,
                    ) -> tuple[Image.Image, np.ndarray | None, set[str]]:
    """Levelling path of _render_frame: one affine bicubic resample (after an integer box
    reduction when shrinking by ≥1.5×, so large sources don't alias)."""
    W, H = img.size
    ow, oh = size
    x0, y0, x1, y1 = frame
    s = ow / (x1 - x0)                   # output px per source px
    c, sn = math.cos(math.radians(lev.deg)), math.sin(math.radians(lev.deg))
    px0, py0 = lev.pivot
    # Output (u, v) → source: p = R(θ)·((x0, y0) + (u, v)/s − pivot) + pivot.
    a, b, d, e = c / s, -sn / s, sn / s, c / s
    cx = px0 + c * (x0 - px0) - sn * (y0 - py0)
    cy = py0 + sn * (x0 - px0) + c * (y0 - py0)
    corners = _frame_corners(frame, lev)
    tol = 1.0 / s
    sides = {side for side, out in zip(SIDES, (corners[:, 1].min() < -tol, corners[:, 1].max() > H + tol,
                                               corners[:, 0].min() < -tol, corners[:, 0].max() > W + tol)) if out}

    factor = max(1, round(1.0 / s)) if s < 2.0 / 3.0 else 1
    margin = 2 * factor + 2
    bx0 = max(0, math.floor(corners[:, 0].min()) - margin)
    by0 = max(0, math.floor(corners[:, 1].min()) - margin)
    bx1 = min(W, math.ceil(corners[:, 0].max()) + margin)
    by1 = min(H, math.ceil(corners[:, 1].max()) + margin)
    rgba = img.mode == "RGBA"
    fill_c = fill if rgba else fill[:3]
    if bx1 - bx0 < 1 or by1 - by0 < 1:
        out = Image.new(img.mode, (ow, oh), fill_c)
    else:
        region = img.crop((bx0, by0, bx1, by1))
        if rgba:
            region = region.convert("RGBa")  # premultiplied: transparent pixels don't bleed colour
        if factor > 1:
            region = region.reduce(factor)
        data = (a / factor, b / factor, (cx - bx0) / factor, d / factor, e / factor, (cy - by0) / factor)
        out = region.transform((ow, oh), Image.Transform.AFFINE, data, resample=_BICUBIC,
                               fillcolor=tuple(round(v * fill[3] / 255) for v in fill[:3]) + (fill[3],)
                               if rgba else fill_c)
        if rgba:
            out = out.convert("RGBA")
    out_mask = None
    if mask is not None:
        mh, mw = mask.shape
        fx, fy = mw / W, mh / H
        # cv2 maps pixel centres: dst (u, v) → src (A·(u + ½, v + ½) + t)·scale − ½.
        m = np.array([[a * fx, b * fx, (cx + 0.5 * (a + b)) * fx - 0.5],
                      [d * fy, e * fy, (cy + 0.5 * (d + e)) * fy - 0.5]], np.float64)
        out_mask = cv2.warpAffine(np.ascontiguousarray(mask, dtype=np.float32), m, (ow, oh),
                                  flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                                  borderMode=cv2.BORDER_CONSTANT, borderValue=0.0)
        out_mask = np.clip(out_mask, 0.0, 1.0)
    return out, out_mask, sides


def _mask_touches(mask: np.ndarray, side: str, corners: np.ndarray, image_size: tuple[int, int]) -> bool:
    """True when the person mask reaches the source's `side` border within the frame's span."""
    mh, mw = mask.shape
    W, H = image_size
    fx, fy = mw / W, mh / H
    if side in ("top", "bottom"):
        lo, hi = max(0.0, corners[:, 0].min()), min(float(W), corners[:, 0].max())
        line = mask[0 if side == "top" else mh - 1, math.floor(lo * fx):math.ceil(hi * fx)]
    else:
        lo, hi = max(0.0, corners[:, 1].min()), min(float(H), corners[:, 1].max())
        line = mask[math.floor(lo * fy):math.ceil(hi * fy), 0 if side == "left" else mw - 1]
    return line.size > 0 and float((line > 0.5).mean()) > EDGE_TOUCH


def _landmark_eyes(face: Face) -> Eyes | None:
    re, le = face.landmarks.get("right_eye"), face.landmarks.get("left_eye")
    return (re, le) if re is not None and le is not None else None


def _country(preset: PassportPreset) -> str:
    return preset.name.split(" — ")[0].strip() or "This authority's"


def make(img: Image.Image, preset: PassportPreset | str, *, background: str | None = None,
         replace_background: bool = True, face_index: int = 0, zoom: float = 1.0,
         offset_x: float = 0.0, offset_y: float = 0.0, level: bool = True,
         analysis: Analysis | None = None) -> tuple[Image.Image, dict]:
    """Frame `img` as a passport/ID photo per `preset` (§5.3).

    `zoom` scales the head (1.0 = middle of the allowed head range); `offset_x`/`offset_y` move
    the subject right/down by that fraction of the output width/height. The eye line sits at
    the middle of its range unless that would crop the crown, in which case it is lowered
    within the range. With `level`, a head roll of 1–15° (measured from the pupils) is levelled
    by rotating about the eye midpoint inside the single resample; a larger roll only warns.
    The head is measured to the top of the hair, or to the top of the skull when the preset
    says `crown_includes_hair=False`.

    Returns (image, info). info keys (stable; pixel values refer to the returned image):
      preset, width, height, dpi           preset id, output size in px, print dpi
      face_found                           bool; False → centred crop, the measurements below are None
      head_ratio                           chin→crown height / height (crown per the preset's definition)
      eye_from_bottom                      eye line from the bottom edge / height
      lines                                {"crown", "chin", "eye"}: y of the guide lines in output px, or None
      bands                                preset ranges {"head_min", "head_max", "eye_min", "eye_max",
                                           "top_min", "top_max"} (fractions of height; eye from the bottom)
      background_edit_allowed              preset flag (False → replacing the background is warned about)
      crown_includes_hair                  preset flag (False → crown is the skull top estimate)
      roll_deg                             measured head roll before levelling (+ = clockwise), or None
      levelled_deg                         rotation applied to level the head (0 when none); applied in
                                           0.1° steps, so this is exactly the rotation used
      face_box_out                         {"box": [x, y, w, h], "crown_y", "chin_y", "eye_y", "center_x",
                                           "top_y" (top of the hair)} in output px, or None
      source_frame                         where the output frame sits on `img` (both branches):
                                           {"width": W, "height": H,   size of `img` in px
                                            "center": [cx, cy],        frame centre in `img` px (2 dp)
                                            "size": [fw, fh],          frame size in `img` px (2 dp; fw/fh = out w/h)
                                            "angle": deg}              frame rotated `deg` clockwise (y down) about
                                                                       its centre; == levelled_deg (0: no levelling)
                                           Output point (u, v) (continuous, 0…width × 0…height) samples `img` at
                                           center + R(angle)·((u − width/2)·fw/width, (v − height/2)·fh/height),
                                           R(θ) = [[cos θ, −sin θ], [sin θ, cos θ]]. It may extend beyond `img`.
      warnings                             list of user-facing strings
    """
    if not isinstance(img, Image.Image):
        raise TypeError("make() expects a PIL image")
    preset = _resolve_preset(preset)
    if not 0.25 <= zoom <= 4.0:
        raise ValueError("zoom must be between 0.25 and 4")
    if not (-1.0 <= offset_x <= 1.0 and -1.0 <= offset_y <= 1.0):
        raise ValueError("offset_x and offset_y must be between -1 and 1")
    if int(face_index) != face_index or face_index < 0:
        raise ValueError("face_index must be a non-negative integer")
    ow, oh = preset.output_size()
    if ow < 1 or oh < 1:
        raise ValueError("preset output size must be at least 1×1 px")
    bg_spec = preset.background if background is None else background
    bg = segment.parse_color(bg_spec)
    want_alpha = bg[3] < 255
    warnings: list[str] = []

    if analysis is None or (analysis.size is not None and tuple(analysis.size) != img.size):
        analysis = analyze(img)
    work_mode = "RGBA" if ("A" in img.getbands() or want_alpha) else "RGB"
    work = img if img.mode == work_mode else img.convert(work_mode)
    W, H = img.size

    has_head_rule = preset.head_max > 0
    info: dict = {
        "preset": preset.id, "width": ow, "height": oh, "dpi": preset.dpi,
        "face_found": bool(analysis.faces), "head_ratio": None, "eye_from_bottom": None, "lines": None,
        "bands": {"head_min": preset.head_min if has_head_rule else None,
                  "head_max": preset.head_max if has_head_rule else None,
                  "eye_min": preset.eye_min, "eye_max": preset.eye_max,
                  "top_min": preset.top_min, "top_max": preset.top_max},
        "background_edit_allowed": preset.background_edit_allowed,
        "crown_includes_hair": preset.crown_includes_hair,
        "roll_deg": None, "levelled_deg": 0.0, "face_box_out": None, "source_frame": None, "warnings": warnings,
    }

    if not analysis.faces:
        warnings.append("No face detected — used a centred crop; check the framing and background manually."
                        if face_mod.available() else
                        "Face detection is unavailable — used a centred crop; check the framing and background manually.")
        aspect = ow / oh
        cw, ch = (W, W / aspect) if W / H <= aspect else (H * aspect, H)
        fw, fh = cw / zoom, ch / zoom
        # Same convention as the face branch: the subject moves right/down by offset × frame size.
        cx, cy = W / 2 - offset_x * fw, H / 2 - offset_y * fh
        frame = (cx - fw / 2, cy - fh / 2, cx + fw / 2, cy + fh / 2)
        out, _, sides = _render_frame(work, frame, (ow, oh), bg, None)
        if sides:
            warnings.append("The frame extends beyond the photo; the gap was filled with the background colour.")
        scale = ow / (frame[2] - frame[0])
        info["source_frame"] = _source_frame(frame, _Level(), (ow, oh), img.size)
    else:
        idx = int(face_index)
        if idx >= len(analysis.faces):
            warnings.append(f"Face #{idx + 1} not found ({len(analysis.faces)} detected); using the largest face.")
            idx = 0
        if len(analysis.faces) > 1:
            warnings.append(f"{len(analysis.faces)} faces detected; framing face #{idx + 1} (largest first).")
        face = analysis.faces[idx]

        eyes = analysis.eyes[idx] if analysis.eyes is not None and idx < len(analysis.eyes) \
            else face_mod.refine_eyes(img, face)
        roll = face_mod.roll_deg(eyes)
        angle = 0.0
        if roll is not None:
            info["roll_deg"] = round(roll, 1)
            if abs(roll) > LEVEL_MAX_DEG:
                warnings.append(f"The head is tilted by about {abs(roll):.0f}°, too much to level automatically; "
                                "retake the photo with the head upright.")
            elif level and abs(roll) >= LEVEL_MIN_DEG:
                angle = round(roll, 1)  # levelled_deg / source_frame.angle report exactly this
            elif not level and abs(roll) > TILT_WARN_DEG:
                warnings.append(f"The head is tilted by about {abs(roll):.0f}°; turn on levelling or "
                                "straighten the photo.")
        else:
            coarse = face_mod.roll_deg(_landmark_eyes(face))
            if coarse is not None and abs(coarse) >= COARSE_TILT_DEG:
                warnings.append("The head looks tilted (the eyes could not be measured precisely); "
                                "make sure it is upright.")
        info["levelled_deg"] = angle
        geo = head_geometry(face, analysis.mask, image_size=img.size, roll=angle,
                            crown_includes_hair=preset.crown_includes_hair)
        lev = _Level(angle, (geo["center_x"], geo["eye_y"]))

        head_min, head_max = (preset.head_min, preset.head_max) if has_head_rule else DEFAULT_HEAD_RATIO
        target_h = oh * (head_min + head_max) / 2.0 * zoom
        scale = target_h / geo["head_h"]
        visible_h = (geo["chin_y"] - geo["hair_y"]) * scale  # hair included, even when the spec excludes it
        eye_mid = _mid(preset.eye_min, preset.eye_max)
        top_mid = _mid(preset.top_min, preset.top_max)
        if eye_mid is not None:
            eye_out = oh * (1.0 - eye_mid)
            # Mid eye line + large head can push the crown out of frame; lower the eye line,
            # but never below the allowed range, to keep a small margin above the head.
            crown_above_eye = (geo["eye_y"] - geo["hair_y"]) * scale
            min_top = oh * (preset.top_min if preset.top_min is not None else MIN_TOP_MARGIN)
            if eye_out - crown_above_eye < min_top and preset.eye_min is not None:
                eye_out = min(oh * (1.0 - preset.eye_min), min_top + crown_above_eye)
            top = geo["eye_y"] - (eye_out + offset_y * oh) / scale
        elif top_mid is not None:
            top = geo["crown_y"] - (oh * top_mid + offset_y * oh) / scale
        else:
            # Share the free space 45/55 above the hair and below the chin; with very big hair keep
            # the chin clear of the bottom edge rather than the hair clear of the top.
            margin = min((oh - visible_h) * 0.45, oh - visible_h - 0.275 * max(0.0, oh - target_h))
            top = geo["hair_y"] - (margin + offset_y * oh) / scale
        left = geo["center_x"] - (ow / 2.0 + offset_x * ow) / scale
        frame = (left, top, left + ow / scale, top + oh / scale)

        replace = replace_background and analysis.mask is not None
        if replace_background and analysis.mask is None:
            warnings.append("Background replacement is unavailable (segmentation model missing); "
                            "the original background was kept.")
        out, out_mask, sides = _render_frame(work, frame, (ow, oh), bg, analysis.mask, lev)
        info["source_frame"] = _source_frame(frame, lev, (ow, oh), img.size)
        if replace:
            out = segment.composite(out, out_mask, bg)
            if not preset.background_edit_allowed:
                warnings.append(f"{_country(preset)} rules don't allow digitally altered backgrounds — "
                                "photograph against a plain light wall instead.")

        if geo["crown_clipped"]:
            warnings.append("The top of the head is cut off in the source photo, so the head size may be wrong. "
                            "Use a photo with space above the hair.")
        if geo["chin_clipped"]:
            warnings.append("The chin is cut off in the source photo, so the head size may be wrong; "
                            "use a photo that shows the neck and shoulders.")
        if sides and not replace:
            warnings.append("The frame extends beyond the photo; the gap was filled with the background colour. "
                            "Use a photo with more space around the head.")
        elif sides:
            # With a replaced background the fill is invisible unless the person runs into that edge.
            corners = _frame_corners(frame, lev)
            cut = [s for s in SIDES if s in sides and _mask_touches(analysis.mask, s, corners, img.size)
                   and not (s == "top" and geo["crown_clipped"]) and not (s == "bottom" and geo["chin_clipped"])]
            if cut:
                warnings.append(f"The photo cuts off the person at the {_join(cut)}; the gap was filled with the "
                                "background colour. Use a photo with more space around the person.")

        def oy(v: float) -> float:
            return (v - top) * scale

        def ox(v: float) -> float:
            return (v - left) * scale

        crown_o, chin_o, eye_o, hair_o = oy(geo["crown_y"]), oy(geo["chin_y"]), oy(geo["eye_y"]), oy(geo["hair_y"])
        head_ratio = (chin_o - crown_o) / oh
        eye_from_bottom = 1.0 - eye_o / oh
        fx, fy, fw, fh = face.box
        bcx, bcy = lev.to_level(fx + fw / 2.0, fy + fh / 2.0)
        info["head_ratio"] = round(head_ratio, 4)
        info["eye_from_bottom"] = round(eye_from_bottom, 4)
        info["lines"] = {"crown": round(crown_o, 1), "chin": round(chin_o, 1), "eye": round(eye_o, 1)}
        info["face_box_out"] = {
            "box": [round(ox(bcx - fw / 2.0)), round(oy(bcy - fh / 2.0)), round(fw * scale), round(fh * scale)],
            "crown_y": round(crown_o, 1), "chin_y": round(chin_o, 1), "eye_y": round(eye_o, 1),
            "center_x": round(ox(geo["center_x"]), 1), "top_y": round(hair_o, 1),
        }
        if has_head_rule and not _in_range(head_ratio, preset.head_min, preset.head_max):
            warnings.append(f"Head height is {head_ratio:.0%} of the photo; the spec requires "
                            f"{preset.head_min:.0%}–{preset.head_max:.0%}. Adjust the zoom.")
        if eye_mid is not None and not _in_range(eye_from_bottom, preset.eye_min, preset.eye_max):
            lo, hi = preset.eye_min or eye_mid, preset.eye_max or eye_mid
            warnings.append(f"Eye line is {eye_from_bottom:.0%} from the bottom; the spec requires {lo:.0%}–{hi:.0%}.")
        if top_mid is not None and not _in_range(crown_o / oh, preset.top_min, preset.top_max):
            warnings.append("The space above the head is outside the spec; adjust the vertical position.")
        if hair_o < 0:
            warnings.append("The top of the head is cut off; reduce the zoom or move the subject down.")
        if chin_o > oh:
            warnings.append("The chin is cut off; reduce the zoom or move the subject up.")
        if out_mask is not None:
            side = _side_clipping(out_mask, hair_o, chin_o)
            if side:
                warnings.append(f"The head (with hair) is wider than the photo frame and is cut at the {_join(side)} "
                                "edge; reduce the zoom or move the subject sideways.")

    if scale > 1.05:
        warnings.append(f"Low resolution: the photo is enlarged {scale:.1f}× to {ow}×{oh} px; "
                        "use a larger or closer photo for sharp prints.")
    if out.mode == "RGBA" and not want_alpha:
        flat = Image.new("RGB", out.size, bg[:3])
        flat.paste(out, mask=out.getchannel("A"))
        out = flat
    out.info["dpi"] = (preset.dpi, preset.dpi)
    return out, info


def _side_clipping(out_mask: np.ndarray, top_o: float, chin_o: float) -> list[str]:
    """Frame sides ("left"/"right") the head touches on more than SIDE_TOUCH_ROWS of its rows."""
    oh, ow = out_mask.shape
    band = max(2, round(0.01 * ow))
    r0, r1 = max(0, int(top_o)), min(oh, int(chin_o))
    if r1 - r0 < 4:
        return []
    rows = out_mask[r0:r1] > 0.5
    return [name for name, cols in (("left", rows[:, :band]), ("right", rows[:, -band:]))
            if float(cols.any(axis=1).mean()) > SIDE_TOUCH_ROWS]


# --- print sheets (§5.4) -------------------------------------------------------------------


def _grid(paper_w: float, paper_h: float, pw: float, ph: float, gap: float, margin: float) -> tuple[int, int]:
    cols = math.floor((paper_w - 2 * margin + gap) / (pw + gap) + 1e-9)
    rows = math.floor((paper_h - 2 * margin + gap) / (ph + gap) + 1e-9)
    return max(0, cols), max(0, rows)


def _paper_dims(paper_w: float, paper_h: float, orientation: str) -> tuple[float, float]:
    short, long = sorted((paper_w, paper_h))
    return (short, long) if orientation == "portrait" else (long, short)


def _fit_photo(img: Image.Image, size: tuple[int, int], warnings: list[str]) -> Image.Image:
    """Flatten onto white and cover-resize to `size` (centre crop if the aspect differs)."""
    if "A" in img.getbands() or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.getchannel("A"))
        img = flat
    else:
        img = img.convert("RGB")
    if img.size == size:
        return img
    tw, th = size
    w, h = img.size
    src_aspect, dst_aspect = w / h, tw / th
    if abs(src_aspect / dst_aspect - 1.0) > 0.01:
        warnings.append("The photo's aspect ratio differs from the print size; it was centre-cropped.")
    if src_aspect > dst_aspect:
        cw = h * dst_aspect
        box = ((w - cw) / 2, 0.0, (w + cw) / 2, float(h))
    else:
        ch = w / dst_aspect
        box = (0.0, (h - ch) / 2, float(w), (h + ch) / 2)
    return img.resize(size, _LANCZOS, box=box, reducing_gap=3.0)


def sheet(img: Image.Image, *, paper: str = "4x6", orientation: str = "auto",
          photo_width_mm: float | None = None, photo_height_mm: float | None = None,
          dpi: int | None = None, gap_mm: float = 2, margin_mm: float = 4, cut_lines: bool = True,
          copies: int = 0, src_dpi: float | tuple[float, float] | None = None) -> tuple[Image.Image, dict]:
    """Tile `img` onto a sheet of paper for printing (§5.4). `copies=0` fits as many as possible
    (at most MAX_COPIES); photos smaller than MIN_PHOTO_MM on a side are rejected.

    `dpi=None` prints at the source dpi (e.g. a 400-dpi preset) clamped to 300–600, else 300.
    The layout maximises the copy count; each axis then gets as much of `gap_mm` as its spare
    paper allows (info: gap_x_mm/gap_y_mm; gap_mm is the spacing the count was computed with).
    """
    p = PAPERS.get(str(paper).lower())
    if p is None:
        raise ValueError(f"unknown paper {paper!r}; choose one of: {', '.join(PAPERS)}")
    if orientation not in ("auto", "portrait", "landscape"):
        raise ValueError("orientation must be auto, portrait or landscape")
    if dpi is None:
        dpi = sheet_dpi(src_dpi)
    if int(dpi) != dpi or not 72 <= dpi <= 1200:
        raise ValueError("dpi must be an integer between 72 and 1200")
    if not (0 <= gap_mm <= 50 and 0 <= margin_mm <= 50):
        raise ValueError("gap_mm and margin_mm must be between 0 and 50")
    if int(copies) != copies or not 0 <= copies <= MAX_COPIES:
        raise ValueError(f"copies must be an integer between 0 and {MAX_COPIES}")
    dpi, copies = int(dpi), int(copies)
    warnings: list[str] = []

    # Photo size in mm: explicit, one side + image aspect, or image px at its DPI.
    w_px, h_px = img.size
    if photo_width_mm is not None and photo_width_mm <= 0 or photo_height_mm is not None and photo_height_mm <= 0:
        raise ValueError("photo size must be positive")
    if photo_width_mm and photo_height_mm:
        pw, ph = float(photo_width_mm), float(photo_height_mm)
    elif photo_width_mm:
        pw = float(photo_width_mm)
        ph = pw * h_px / w_px
    elif photo_height_mm:
        ph = float(photo_height_mm)
        pw = ph * w_px / h_px
    else:
        dx, dy = src_dpi if isinstance(src_dpi, (tuple, list)) else (src_dpi, src_dpi)
        dx = float(dx) if dx and dx > 0 else 300.0
        dy = float(dy) if dy and dy > 0 else dx
        pw, ph = w_px / dx * MM_PER_INCH, h_px / dy * MM_PER_INCH
    if min(pw, ph) < MIN_PHOTO_MM:
        raise ValueError(f"a {pw:.1f}×{ph:.1f} mm photo is too small to print (minimum {MIN_PHOTO_MM:g} mm per "
                         "side); set photo_width_mm/photo_height_mm")

    m0 = 0.0 if p.borderless else float(margin_mm)
    orients = ["portrait", "landscape"] if orientation == "auto" else [orientation]

    def best(gap: float, margin: float) -> tuple[int, str, tuple[int, int]]:
        top = (-1, "", (0, 0))
        for o in orients:  # portrait first: wins ties
            cols, rows = _grid(*_paper_dims(p.width_mm, p.height_mm, o), pw, ph, gap, margin)
            if cols * rows > top[0]:
                top = (cols * rows, o, (cols, rows))
        return top

    max_n = best(0.0, m0)[0]
    if max_n < 1:
        raise ValueError(f"a {pw:.1f}×{ph:.1f} mm photo does not fit on {p.name} paper; "
                         "set photo_width_mm/photo_height_mm or a higher source DPI")
    want = min(max_n, MAX_COPIES) if copies == 0 else min(copies, max_n)
    if copies > max_n:
        warnings.append(f"Only {max_n} copies fit on {p.name} paper (requested {copies}).")
    elif copies == 0 and max_n > MAX_COPIES:
        warnings.append(f"{max_n} copies would fit on {p.name} paper; the sheet holds the maximum of {MAX_COPIES}.")

    # Spacing is a preference: relax gap/margin only as far as needed to reach `want`.
    for gap, margin in dict.fromkeys([(float(gap_mm), float(margin_mm)), (float(gap_mm), m0), (0.0, m0)]):
        count, orient, (cols, rows) = best(gap, margin)
        if count >= want:
            break

    k = dpi / MM_PER_INCH
    paper_w_mm, paper_h_mm = _paper_dims(p.width_mm, p.height_mm, orient)
    sheet_w, sheet_h = round(paper_w_mm * k), round(paper_h_mm * k)
    photo_w, photo_h = max(1, round(pw * k)), max(1, round(ph * k))
    used_cols = min(cols, want)
    used_rows = math.ceil(want / used_cols)

    def axis_gap(n: int, size_mm: float, paper_mm: float) -> float:
        """Up to gap_mm between photos, as far as this axis' spare paper allows."""
        if n < 2:
            return 0.0
        spare = paper_mm - 2 * margin - n * size_mm
        return round(max(0.0, min(float(gap_mm), spare / (n - 1))), 2)

    def axis(n: int, size: int, total: int, g_mm: float) -> tuple[int, int]:
        g = round(g_mm * k)
        if n > 1 and n * size + (n - 1) * g > total:  # px rounding: shrink the gap, never overlap
            g = max(0, (total - n * size) // (n - 1))
        return g, max(0, (total - (n * size + (n - 1) * g)) // 2)

    gx_mm, gy_mm = axis_gap(used_cols, pw, paper_w_mm), axis_gap(used_rows, ph, paper_h_mm)
    gap_x, x0 = axis(used_cols, photo_w, sheet_w, gx_mm)
    gap_y, y0 = axis(used_rows, photo_h, sheet_h, gy_mm)

    photo = _fit_photo(img, (photo_w, photo_h), warnings)
    out = Image.new("RGB", (sheet_w, sheet_h), (255, 255, 255))
    # One row strip, pasted once per row: O(cols + rows) pastes whatever the copy count.
    strip = Image.new("RGB", (used_cols * photo_w + (used_cols - 1) * gap_x, photo_h), (255, 255, 255))
    for c in range(used_cols):
        strip.paste(photo, (c * (photo_w + gap_x), 0))
    for r in range(used_rows):
        n = min(used_cols, want - r * used_cols)
        row = strip if n == used_cols else strip.crop((0, 0, n * photo_w + (n - 1) * gap_x, photo_h))
        out.paste(row, (x0, y0 + r * (photo_h + gap_y)))

    if cut_lines:
        draw = ImageDraw.Draw(out)
        for pos in _cut_positions(used_cols, photo_w, gap_x, x0, sheet_w):
            draw.line([(pos, 0), (pos, sheet_h - 1)], fill=CUT_LINE_COLOR, width=1)
        for pos in _cut_positions(used_rows, photo_h, gap_y, y0, sheet_h):
            draw.line([(0, pos), (sheet_w - 1, pos)], fill=CUT_LINE_COLOR, width=1)

    out.info["dpi"] = (dpi, dpi)
    info = {
        "paper": p.id, "orientation": orient, "cols": used_cols, "rows": used_rows, "copies": want,
        "photo_mm": [round(pw, 2), round(ph, 2)], "photo_px": [photo_w, photo_h],
        "gap_mm": gap, "gap_x_mm": gx_mm, "gap_y_mm": gy_mm, "margin_mm": margin, "dpi": dpi,
        "width": sheet_w, "height": sheet_h,
        "warnings": warnings,
    }
    return out, info


def _cut_positions(n: int, size: int, gap: int, start: int, total: int) -> list[int]:
    """Cut-line coordinates along one axis: through each gap (or on shared edges) plus the outer edges."""
    positions = []
    for i in range(1, n):
        edge = start + i * size + (i - 1) * gap  # first px after photo i-1
        positions.append(edge + gap // 2 if gap else edge)
    end = start + n * size + (n - 1) * gap
    before, after = start - 1 - gap // 2, end + gap // 2
    if before >= 0:
        positions.append(before)
    elif start - 1 >= 0:
        positions.append(start - 1)
    if after < total:
        positions.append(after)
    elif end < total:
        positions.append(end)
    return sorted(set(positions))


def sheet_dpi(src_dpi: float | tuple[float, float] | None) -> int:
    """Print dpi for a sheet without an explicit one: the source dpi clamped to 300–600, else 300."""
    d = src_dpi[0] if isinstance(src_dpi, (tuple, list)) else src_dpi
    if d is None or not d > 0:
        return SHEET_DPI_DEFAULT
    return int(min(SHEET_DPI_AUTO[1], max(SHEET_DPI_AUTO[0], round(float(d)))))


__all__ = ["Analysis", "analyze", "head_geometry", "make", "sheet", "sheet_dpi", "ANALYSIS_MASK_MAX"]
