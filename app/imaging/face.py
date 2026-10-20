"""Face detection: YuNet (OpenCV DNN) with an optional Haar-cascade fallback.

Detection runs on a downscaled copy (long side <= 1024 px) and results are mapped back to
the coordinates of the image passed in. Faces are returned largest first. `refine_small`
re-detects faces that were tiny at detection scale, and `refine_eyes`/`roll_deg` measure head
roll from the pupils (YuNet's eye landmarks are pulled towards an upright template).
"""
from __future__ import annotations

import math
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

YUNET_FILE = "face_detection_yunet_2023mar.onnx"
HAAR_FILE = "haarcascade_frontalface_default.xml"
DETECT_MAX_SIDE = 1024
MIN_SCORE = 0.7
REFINE_BELOW = 48       # px face height at detection scale below which `refine_small` re-detects
REFINE_SIDE = 640       # the re-detection crop is upscaled to at least this long side
REFINE_CROP = 4.0       # crop side in face sizes
EYE_NORM_WIDTH = 128.0  # pupil search runs with the face normalised to this width
EYE_WINDOW = 0.12       # search radius around each YuNet eye, as a fraction of the face width
# YuNet output row: x, y, w, h, then 5 landmarks (x, y) in this order, then the score.
# "right"/"left" are the subject's own sides (right_eye appears on the image's left).
LANDMARK_NAMES = ("right_eye", "left_eye", "nose", "mouth_right", "mouth_left")

_lock = threading.Lock()
_yunet: cv2.FaceDetectorYN | None = None
_yunet_failed = False
_haar = None
_haar_failed = False


@dataclass
class Face:
    """A detected face in image pixel coordinates."""

    box: tuple[int, int, int, int]  # x, y, w, h (clamped to the image)
    score: float
    landmarks: dict[str, tuple[float, float]] = field(default_factory=dict)  # empty for Haar
    raw_box: tuple[float, float, float, float] | None = None  # detector box before clamping (may leave the image)

    @property
    def bottom(self) -> float:
        """Bottom edge of the unclamped detector box (≈ chin), else of the clamped box."""
        if self.raw_box is not None:
            return self.raw_box[1] + self.raw_box[3]
        return float(self.box[1] + self.box[3])

    @property
    def area(self) -> int:
        return self.box[2] * self.box[3]

    def eye_center(self) -> tuple[float, float] | None:
        """Midpoint between the eyes, or None without landmarks."""
        re, le = self.landmarks.get("right_eye"), self.landmarks.get("left_eye")
        if re is None or le is None:
            return None
        return (re[0] + le[0]) / 2.0, (re[1] + le[1]) / 2.0

    def to_dict(self) -> dict:
        return {
            "box": [int(v) for v in self.box],
            "score": round(float(self.score), 4),
            "landmarks": {k: [round(float(x), 1), round(float(y), 1)] for k, (x, y) in self.landmarks.items()},
        }


def models_dir() -> Path:
    return Path(os.environ.get("HEYP_MODELS_DIR", "/opt/heyp/models"))


def _get_yunet() -> cv2.FaceDetectorYN | None:
    """Create the YuNet detector once; caller must hold `_lock`."""
    global _yunet, _yunet_failed
    if _yunet is not None or _yunet_failed:
        return _yunet
    path = models_dir() / YUNET_FILE
    if not path.is_file():
        _yunet_failed = True
        return None
    logging = cv2.utils.logging
    # OpenCV 5's new DNN engine warns "Targets are not supported" on every create.
    prev = logging.setLogLevel(logging.LOG_LEVEL_ERROR)
    try:
        # Low internal threshold; callers filter by their own min_score.
        _yunet = cv2.FaceDetectorYN.create(str(path), "", (320, 320), 0.5, 0.3, 5000)
    except cv2.error:
        _yunet_failed = True
    finally:
        logging.setLogLevel(prev)
    return _yunet


def _get_haar():
    """Haar cascade fallback; unavailable in OpenCV 5 wheels (no CascadeClassifier/XML)."""
    global _haar, _haar_failed
    if _haar is not None or _haar_failed:
        return _haar
    cascade_dir = getattr(getattr(cv2, "data", None), "haarcascades", None)
    if not hasattr(cv2, "CascadeClassifier") or not cascade_dir:
        _haar_failed = True
        return None
    path = Path(cascade_dir) / HAAR_FILE
    clf = cv2.CascadeClassifier(str(path)) if path.is_file() else None
    if clf is None or clf.empty():
        _haar_failed = True
        return None
    _haar = clf
    return _haar


def backend() -> str | None:
    """Name of the detector in use: "yunet", "haar" or None."""
    with _lock:
        if _get_yunet() is not None:
            return "yunet"
        if _get_haar() is not None:
            return "haar"
    return None


def available() -> bool:
    """True when any face detector can be used."""
    return backend() is not None


def _to_rgb(img: Image.Image) -> Image.Image:
    """RGB copy; transparent areas become white so they don't read as dark blobs."""
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    return img.convert("RGB")


def detect(img: Image.Image, *, min_score: float = MIN_SCORE, max_side: int = DETECT_MAX_SIDE) -> list[Face]:
    """Detect faces in `img`; returns faces sorted by box area (largest first)."""
    if not isinstance(img, Image.Image):
        raise TypeError("detect() expects a PIL image")
    if not 0.0 <= min_score <= 1.0:
        raise ValueError("min_score must be within [0, 1]")
    width, height = img.size
    if width < 16 or height < 16:
        return []
    scale = min(1.0, max_side / max(width, height))
    small = img
    if scale < 1.0:  # downscale before any mode conversion so huge images are never copied full-size
        if small.mode not in ("RGB", "RGBA", "L", "LA"):
            small = small.convert("RGBA" if "transparency" in small.info else "RGB")
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        small = small.resize(size, Image.Resampling.BILINEAR, reducing_gap=2.0)
    small = _to_rgb(small)
    # Exact per-axis factors back to the original image (rounding can differ per axis).
    fx, fy = width / small.width, height / small.height
    bgr = np.ascontiguousarray(np.asarray(small)[:, :, ::-1])

    with _lock:
        yunet = _get_yunet()
        if yunet is not None:
            yunet.setInputSize((small.width, small.height))
            _, rows = yunet.detect(bgr)
            faces = [] if rows is None else [_face_from_yunet(r, fx, fy, width, height) for r in rows]
        else:
            haar = _get_haar()
            faces = [] if haar is None else _detect_haar(haar, bgr, fx, fy, width, height)

    faces = [f for f in faces if f.score >= min_score and f.box[2] > 0 and f.box[3] > 0]
    faces.sort(key=lambda f: f.area, reverse=True)
    return faces


def _clamp_box(x: float, y: float, w: float, h: float, width: int, height: int) -> tuple[int, int, int, int]:
    x0, y0 = max(0, round(x)), max(0, round(y))
    x1, y1 = min(width, round(x + w)), min(height, round(y + h))
    return x0, y0, max(0, x1 - x0), max(0, y1 - y0)


def _face_from_yunet(row: np.ndarray, fx: float, fy: float, width: int, height: int) -> Face:
    x, y, w, h = (float(v) for v in row[:4])
    landmarks = {
        name: (float(row[4 + 2 * i]) * fx, float(row[5 + 2 * i]) * fy)
        for i, name in enumerate(LANDMARK_NAMES)
    }
    box = _clamp_box(x * fx, y * fy, w * fx, h * fy, width, height)
    return Face(box=box, score=float(row[14]), landmarks=landmarks, raw_box=(x * fx, y * fy, w * fx, h * fy))


def _detect_haar(haar, bgr: np.ndarray, fx: float, fy: float, width: int, height: int) -> list[Face]:
    gray = cv2.equalizeHist(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY))
    min_side = max(24, min(gray.shape[:2]) // 12)
    rects = haar.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(min_side, min_side))
    # Haar gives no confidence; a fixed nominal score keeps it above the default threshold.
    return [Face(box=_clamp_box(x * fx, y * fy, w * fx, h * fy, width, height), score=0.75)
            for (x, y, w, h) in rects]


def refine_small(img: Image.Image, faces: list[Face], *, limit: int = 3,
                 max_side: int = DETECT_MAX_SIDE) -> list[Face]:
    """Re-detect the `limit` largest faces that were tiny at detection scale on an upscaled crop.

    When a face is under ~48 px tall at the ≤1024 px detection scale, YuNet's box bottom (the chin
    estimate) drifts by up to ~15 % of the head height; a second pass on a crop around it fixes that.
    Faces that cannot be re-detected are kept as they were. Returns faces largest first.
    """
    det_scale = min(1.0, max_side / max(img.size))
    out = list(faces)
    for i, f in enumerate(out[:limit]):
        if f.box[3] * det_scale < REFINE_BELOW and f.landmarks:
            out[i] = _redetect(img, f, max_side) or f
    out.sort(key=lambda f: f.area, reverse=True)
    return out


def _redetect(img: Image.Image, f: Face, max_side: int) -> Face | None:
    x, y, w, h = f.raw_box or f.box
    side = REFINE_CROP * max(w, h)
    cx, cy = x + w / 2.0, y + h / 2.0
    x0, y0 = max(0, math.floor(cx - side / 2)), max(0, math.floor(cy - side / 2))
    x1, y1 = min(img.width, math.ceil(cx + side / 2)), min(img.height, math.ceil(cy + side / 2))
    if x1 - x0 < 16 or y1 - y0 < 16:
        return None
    crop = img.crop((x0, y0, x1, y1))
    k = REFINE_SIDE / max(crop.size)
    if k > 1.0:
        crop = crop.resize((round(crop.width * k), round(crop.height * k)), Image.Resampling.BICUBIC)
    kx, ky = crop.width / (x1 - x0), crop.height / (y1 - y0)
    best, best_d = None, 0.5 * max(w, h)
    for g in detect(crop, max_side=max_side):
        gx, gy, gw, gh = g.raw_box or g.box
        d = math.hypot((gx + gw / 2) / kx + x0 - cx, (gy + gh / 2) / ky + y0 - cy)
        if d < best_d:
            best, best_d = g, d
    if best is None:
        return None
    gx, gy, gw, gh = best.raw_box or best.box
    raw = (gx / kx + x0, gy / ky + y0, gw / kx, gh / ky)
    landmarks = {n: (px / kx + x0, py / ky + y0) for n, (px, py) in best.landmarks.items()}
    return Face(box=_clamp_box(*raw, img.width, img.height), score=best.score, landmarks=landmarks, raw_box=raw)


def refine_eyes(img: Image.Image, f: Face) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Pupil centres (subject's right eye, left eye) near the YuNet eye landmarks, or None.

    Takes the darkest point of a Gaussian-blurred luminance window around each landmark, with the
    face normalised to EYE_NORM_WIDTH px. Returns None when a minimum sits on its window edge, is
    not clearly darker than its window (closed eyes, glare, glasses) or the refined eye distance or
    midpoint disagrees with YuNet's.
    """
    re, le = f.landmarks.get("right_eye"), f.landmarks.get("left_eye")
    fw = (f.raw_box or f.box)[2]
    if re is None or le is None or fw < 8:
        return None
    r = EYE_WINDOW * fw
    pad = r + 2.0
    x0 = max(0, math.floor(min(re[0], le[0]) - pad))
    y0 = max(0, math.floor(min(re[1], le[1]) - pad))
    x1 = min(img.width, math.ceil(max(re[0], le[0]) + pad))
    y1 = min(img.height, math.ceil(max(re[1], le[1]) + pad))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    crop = _to_rgb(img.crop((x0, y0, x1, y1))).convert("L")
    k = EYE_NORM_WIDTH / fw
    size = (max(1, round(crop.width * k)), max(1, round(crop.height * k)))
    crop = crop.resize(size, Image.Resampling.BOX if k < 1 else Image.Resampling.BILINEAR)
    kx, ky = crop.width / (x1 - x0), crop.height / (y1 - y0)
    radius = max(3, round(r * k))
    luma = cv2.GaussianBlur(np.asarray(crop, np.float32), (0, 0), max(1.0, radius / 6.0))

    pupils = []
    for ex, ey in (re, le):
        cx, cy = round((ex - x0) * kx), round((ey - y0) * ky)
        wx0, wy0 = max(0, cx - radius), max(0, cy - radius)
        win = luma[wy0:cy + radius + 1, wx0:cx + radius + 1]
        if win.shape[0] < 3 or win.shape[1] < 3:
            return None
        iy, ix = np.unravel_index(int(np.argmin(win)), win.shape)
        if min(ix, iy, win.shape[1] - 1 - ix, win.shape[0] - 1 - iy) < 0.15 * radius:
            return None
        if float(np.median(win)) - float(win[iy, ix]) < 15.0:
            return None
        sx, sy = ix + _vertex(win[iy, ix - 1:ix + 2]), iy + _vertex(win[iy - 1:iy + 2, ix])
        pupils.append(((wx0 + sx + 0.5) / kx + x0, (wy0 + sy + 0.5) / ky + y0))
    (ax, ay), (bx, by) = pupils
    yunet_d = math.hypot(le[0] - re[0], le[1] - re[1])
    if yunet_d <= 0 or not 0.8 <= math.hypot(bx - ax, by - ay) / yunet_d <= 1.25:
        return None
    if math.hypot((ax + bx - re[0] - le[0]) / 2, (ay + by - re[1] - le[1]) / 2) > 0.1 * fw:
        return None
    return (ax, ay), (bx, by)


def _vertex(v: np.ndarray) -> float:
    """Sub-pixel offset of a parabola's vertex through three samples (0 when degenerate)."""
    if v.size != 3:
        return 0.0
    denom = float(v[0] - 2 * v[1] + v[2])
    return 0.0 if denom <= 1e-6 else max(-0.5, min(0.5, 0.5 * float(v[0] - v[2]) / denom))


def roll_deg(eyes: tuple[tuple[float, float], tuple[float, float]] | None) -> float | None:
    """Head roll in degrees from (right_eye, left_eye) pupils; positive = turned clockwise in the image."""
    if eyes is None:
        return None
    (ax, ay), (bx, by) = eyes
    return math.degrees(math.atan2(by - ay, bx - ax))


__all__ = ["Face", "detect", "available", "backend", "refine_small", "refine_eyes", "roll_deg",
           "LANDMARK_NAMES", "models_dir"]
