"""Licence-plate detection with open-image-models YOLOv9-t-640 (MIT; end-to-end ONNX, NMS in the graph).

Used only to *suggest* boxes for the blur tool — the user reviews them — so recall matters more than
precision. Two passes (research: whole image alone misses small distant plates):

1. the whole image letterboxed to 640² (~0.1 s), then
2. 1280-px tiles with 20 % overlap when the image is larger than that (~1 s at 12–16 MP).

Detections from all passes are merged: overlapping boxes collapse into one box covering both, so a
plate cut by a tile edge still ends up fully covered. Large images are analysed at ≤ 5120 px.
Model input: RGB/255 letterboxed with grey 114; output rows ``[batch, x1, y1, x2, y2, class, score]``.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

from app.imaging.ortsession import ModelSession

MODEL_FILE = "yolo-v9-t-640-license-plates-end2end.onnx"
INPUT_SIZE = 640
TILE = 1280
TILE_OVERLAP = 0.2
ANALYSIS_MAX_SIDE = 5120
MIN_SCORE = 0.25
LETTERBOX = 114
MAX_TALL = 1.5  # boxes taller than 1.5× their width are dropped (no plate looks like that)

_session = ModelSession(MODEL_FILE, min_bytes=1_000_000, label="licence-plate")


@dataclass(frozen=True)
class Detection:
    """A box in image pixels (x, y, w, h as floats) with a confidence score."""

    x: float
    y: float
    w: float
    h: float
    score: float
    kind: str = "plate"

    @property
    def x2(self) -> float:
        return self.x + self.w

    @property
    def y2(self) -> float:
        return self.y + self.h

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "box": [round(float(v), 1) for v in (self.x, self.y, self.w, self.h)],
                "score": round(float(self.score), 4)}


def available() -> bool:
    return _session.available()


def _rgb(img: Image.Image) -> Image.Image:
    if img.mode == "RGB":
        return img
    if "A" in img.getbands() or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    return img.convert("RGB")


def _letterbox(arr: np.ndarray) -> tuple[np.ndarray, float, float, float]:
    """HxWx3 uint8 → (1x3xSxS float32, scale, pad_x, pad_y)."""
    s = INPUT_SIZE
    h, w = arr.shape[:2]
    r = min(s / h, s / w)
    nw, nh = max(1, round(w * r)), max(1, round(h * r))
    interp = cv2.INTER_AREA if r < 1 else cv2.INTER_LINEAR
    canvas = np.full((s, s, 3), LETTERBOX, np.uint8)
    dw, dh = (s - nw) / 2, (s - nh) / 2
    top, left = int(round(dh - 0.1)), int(round(dw - 0.1))
    canvas[top:top + nh, left:left + nw] = cv2.resize(arr, (nw, nh), interpolation=interp)
    x = np.ascontiguousarray(canvas.transpose(2, 0, 1)[None], dtype=np.float32) * (1.0 / 255.0)
    return x, r, float(left), float(top)


def _detect_array(arr: np.ndarray, ox: float, oy: float, scale: float, min_score: float) -> list[Detection]:
    """Run the model once on ``arr``; boxes are mapped to image px via offset (ox, oy) and ``scale``."""
    x, r, left, top = _letterbox(arr)
    rows = _session.run({_session.input_name(): x})[0]
    h, w = arr.shape[:2]
    out = []
    for row in np.asarray(rows, dtype=np.float64).reshape(-1, 7).tolist():  # plain floats (JSON-safe)
        score = float(row[6])
        if not score >= min_score:  # also drops NaN
            continue
        x1 = min(max((row[1] - left) / r, 0.0), float(w))
        y1 = min(max((row[2] - top) / r, 0.0), float(h))
        x2 = min(max((row[3] - left) / r, 0.0), float(w))
        y2 = min(max((row[4] - top) / r, 0.0), float(h))
        if not all(math.isfinite(v) for v in (x1, y1, x2, y2)):
            continue
        if x2 - x1 < 1 or y2 - y1 < 1 or (y2 - y1) > MAX_TALL * (x2 - x1):
            continue  # plates are wider than tall; tall boxes were tyres, poles and sign posts
        out.append(Detection((x1 + ox) * scale, (y1 + oy) * scale, (x2 - x1) * scale, (y2 - y1) * scale, score))
    return out


def tile_origins(n: int, tile: int = TILE, overlap: float = TILE_OVERLAP) -> list[int]:
    """Start offsets of tiles of ``tile`` px covering ``n`` px with at least ``overlap`` overlap."""
    if n <= tile:
        return [0]
    step = max(1, int(tile * (1.0 - overlap)))
    count = math.ceil((n - tile) / step) + 1
    step = (n - tile) / (count - 1)
    return sorted({int(round(i * step)) for i in range(count)})


def _overlap_small(a: Detection, b: Detection) -> float:
    """Intersection over the smaller box's area."""
    iw = min(a.x2, b.x2) - max(a.x, b.x)
    ih = min(a.y2, b.y2) - max(a.y, b.y)
    if iw <= 0 or ih <= 0:
        return 0.0
    return iw * ih / max(1e-6, min(a.area, b.area))


def merge(dets: Iterable[Detection], threshold: float = 0.5) -> list[Detection]:
    """Greedy merge by score: a box overlapping a kept box (intersection ≥ ``threshold`` of the smaller
    one) is absorbed and the kept box grows to cover both. Returns highest score first."""
    kept: list[Detection] = []
    for d in sorted(dets, key=lambda d: d.score, reverse=True):
        for i, k in enumerate(kept):
            if _overlap_small(d, k) >= threshold:
                x1, y1 = min(d.x, k.x), min(d.y, k.y)
                x2, y2 = max(d.x2, k.x2), max(d.y2, k.y2)
                kept[i] = Detection(x1, y1, x2 - x1, y2 - y1, k.score, k.kind)
                break
        else:
            kept.append(d)
    return kept


def detect(img: Image.Image, *, min_score: float = MIN_SCORE, tiles: bool = True,
           check_cancel: Callable[[], None] | None = None) -> list[Detection]:
    """Plate boxes in ``img`` pixel coordinates, best first. ModelUnavailable if the model is missing."""
    if not isinstance(img, Image.Image):
        raise TypeError("detect() expects a PIL image")
    if not 0.0 <= min_score <= 1.0:
        raise ValueError("min_score must be within [0, 1]")
    width, height = img.size
    if width < 8 or height < 8:
        return []
    _session.get()  # fail early (ModelUnavailable) before any resizing work
    k = min(1.0, ANALYSIS_MAX_SIDE / max(width, height))
    work = img
    if k < 1.0:
        if work.mode not in ("RGB", "RGBA", "L", "LA"):
            work = work.convert("RGBA" if "transparency" in work.info else "RGB")
        work = work.resize((max(1, round(width * k)), max(1, round(height * k))), Image.Resampling.BILINEAR,
                           reducing_gap=2.0)
    arr = np.asarray(_rgb(work))
    back = width / arr.shape[1]  # analysis px → image px
    dets = _detect_array(arr, 0.0, 0.0, back, min_score)
    h, w = arr.shape[:2]
    if tiles and max(h, w) > TILE * 1.25:
        for oy in tile_origins(h):
            for ox in tile_origins(w):
                if check_cancel is not None:
                    check_cancel()
                dets += _detect_array(arr[oy:oy + TILE, ox:ox + TILE], ox, oy, back, min_score)
    return merge(dets)


__all__ = ["Detection", "available", "detect", "merge", "tile_origins", "MODEL_FILE", "MIN_SCORE"]
