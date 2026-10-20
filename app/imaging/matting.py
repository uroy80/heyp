"""General background removal ("any subject") with BiRefNet_lite (MIT) on onnxruntime.

We ship our own re-export of BiRefNet_lite at 1024² with a native ONNX ``DeformConv`` node
(``scripts/export_models/``): the published ONNX emulates deformable convolution with gathers and
needs ~6.4 GB; ours runs in ~1.6 GB peak (arena + memory pattern off) and matches PyTorch to 1e-4.

Two variants exist; ``HEYP_MATTING_MODEL`` picks one (default ``fp32``):

* ``fp32`` — ``birefnet-lite-dc-1024.onnx`` (181 MB), the reference.
* ``int8`` — ``birefnet-lite-dc-1024.int8.onnx`` (84 MB, dynamic QUInt8 MatMul+Conv). Measured in the dev
  image (arm64, ORT 1.31): same speed (3.4 vs 3.3 s at 4 threads, 5.9 vs 5.8 s at 2), same peak (1.60 vs
  1.64 GB), ~140 MB less resident when idle; masks soft-IoU 0.970–0.9997 vs fp32 (thin structures such as
  spokes and rocket fins lose most). Worth it only where disk or idle memory matters;
  ``scripts/fetch_models.py --with-optional`` downloads it.

Pre-processing: RGB (transparent areas on white) resized to 1024² with Lanczos, /255, ImageNet
mean/std. The output is a logit map; the mask is ``sigmoid(logit)`` resized back bilinearly.
At most one BiRefNet inference runs per process (its peak memory is paid once).
"""
from __future__ import annotations

import os

import cv2
import numpy as np
from PIL import Image

from app.imaging.ortsession import ModelSession, release_memory

MODEL_FILES = {"fp32": "birefnet-lite-dc-1024.onnx", "int8": "birefnet-lite-dc-1024.int8.onnx"}
DEFAULT_VARIANT = "fp32"
INPUT_SIZE = 1024
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
PEAK_BYTES = 1_700_000_000  # measured peak of one inference (fp32 and int8 alike), for admission
SECONDS = 3.4               # one inference at 4 threads (5.5 s at 2)

_sessions = {name: ModelSession(file, min_bytes=10_000_000, serialize=True, label=f"background-removal ({name})")
             for name, file in MODEL_FILES.items()}


def variant() -> str:
    """The configured model variant: HEYP_MATTING_MODEL (fp32 | int8), else fp32."""
    raw = os.environ.get("HEYP_MATTING_MODEL", "").strip().lower()
    return raw if raw in MODEL_FILES else DEFAULT_VARIANT


def _session(name: str | None = None) -> ModelSession:
    return _sessions[name or variant()]


def available(name: str | None = None) -> bool:
    """True when the configured (or named) BiRefNet file is installed; never loads it."""
    return _session(name).available()


def _rgb_on_white(img: Image.Image) -> Image.Image:
    if img.mode == "RGB":
        return img
    if "A" in img.getbands() or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    return img.convert("RGB")


def preprocess(img: Image.Image) -> np.ndarray:
    """PIL image → 1×3×1024×1024 float32 network input."""
    small = img
    if small.mode not in ("RGB", "RGBA", "L", "LA"):
        small = small.convert("RGBA" if "transparency" in small.info else "RGB")
    # Resize first, then flatten: a huge image is never copied at full size.
    small = small.resize((INPUT_SIZE, INPUT_SIZE), Image.Resampling.LANCZOS, reducing_gap=3.0)
    x = np.asarray(_rgb_on_white(small), dtype=np.float32) * (1.0 / 255.0)
    x = (x - _MEAN) / _STD
    return np.ascontiguousarray(x.transpose(2, 0, 1)[None], dtype=np.float32)


def raw_mask(img: Image.Image, *, name: str | None = None) -> np.ndarray:
    """Foreground probability at 1024×1024 (float32 in [0, 1]); RuntimeError if the model is unavailable."""
    if not isinstance(img, Image.Image):
        raise TypeError("raw_mask() expects a PIL image")
    session = _session(name)
    x = preprocess(img)
    try:
        logits = session.run({session.input_name(): x})[0]
    finally:
        del x
        release_memory()
    logits = np.asarray(logits, dtype=np.float32).reshape(INPUT_SIZE, INPUT_SIZE)
    # Numerically safe sigmoid (logits reach ±40 on clean backgrounds).
    return (0.5 * (1.0 + np.tanh(0.5 * logits))).astype(np.float32)


def _resize_float(arr: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Resize a float32 HxW mask to (W, H): bilinear up, area down. Always returns a writable array."""
    if (arr.shape[1], arr.shape[0]) == tuple(size):
        return arr.astype(np.float32, copy=False)
    w, h = size
    if max(w, h) < 32767:  # OpenCV's limit
        up = w >= arr.shape[1] and h >= arr.shape[0]
        return cv2.resize(arr, (w, h), interpolation=cv2.INTER_LINEAR if up else cv2.INTER_AREA)
    out = Image.fromarray(np.ascontiguousarray(arr, dtype=np.float32)).resize(size, Image.Resampling.BILINEAR)
    return np.array(out, dtype=np.float32)


def object_mask(img: Image.Image, *, size: tuple[int, int] | None = None, name: str | None = None) -> np.ndarray:
    """Foreground mask of the salient subject (any kind), float32 HxW in [0, 1].

    Matches ``img.size`` unless ``size=(W, H)`` asks for another resolution.
    """
    out_w, out_h = size or img.size
    if out_w < 1 or out_h < 1:
        raise ValueError("mask size must be at least 1×1")
    mask = _resize_float(raw_mask(img, name=name), (out_w, out_h))
    return np.clip(mask, 0.0, 1.0, out=mask)  # interpolation keeps [0, 1]; this only trims float noise


def remove_background(img: Image.Image) -> Image.Image:
    """Cut out the main subject; RGBA with a transparent background."""
    from app.imaging import segment

    return segment.composite(img, object_mask(img), "transparent")


__all__ = ["available", "variant", "object_mask", "raw_mask", "preprocess", "remove_background",
           "MODEL_FILES", "INPUT_SIZE", "PEAK_BYTES"]
