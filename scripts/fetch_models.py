"""Download the ONNX models heyp uses into a directory, verifying each against a pinned size + sha256.

Run at image build time (``python fetch_models.py /models``). Every source, including mirrors from
``HEYP_MODELS_MIRROR`` (comma-separated base URLs tried first), is only accepted when the bytes match the
pin; when no source delivers the pinned file the script exits non-zero, so ``docker build`` fails instead
of shipping an image with face detection or segmentation silently off.
"""
from __future__ import annotations

import hashlib
import os
import sys
import urllib.request
from typing import NamedTuple

CHUNK = 1 << 20
TIMEOUT = 120


class Model(NamedTuple):
    sha256: str
    size: int
    urls: tuple[str, ...]  # tried in order; pinned to immutable revisions where the host allows it


MODELS: dict[str, Model] = {
    "face_detection_yunet_2023mar.onnx": Model(
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        232_589,
        (
            "https://github.com/opencv/opencv_zoo/raw/f12e12798e8314f7c074a6656816c048dcc95b7a"
            "/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
            "https://huggingface.co/opencv/face_detection_yunet/resolve/3cc26e7f1014a5ee5d74a42acee58bafc9d0a310"
            "/face_detection_yunet_2023mar.onnx",
        ),
    ),
    "u2net_human_seg.onnx": Model(
        "01eb6a29a5c4d8edb30b56adad9bb3a2a0535338e480724a213e0acfd2d1c73c",
        175_997_641,
        (
            "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net_human_seg.onnx",
            "https://huggingface.co/tomjackson2023/rembg/resolve/cd3a3d6767a7859efea31ef0f2f373582cf06d82"
            "/u2net_human_seg.onnx",
        ),
    ),
}


class IntegrityError(ValueError):
    """Downloaded bytes don't match the pinned size or digest."""


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def verify(path: str, model: Model) -> bool:
    """True when ``path`` exists with exactly the pinned size and sha256."""
    return os.path.isfile(path) and os.path.getsize(path) == model.size and sha256_of(path) == model.sha256


def mirror_urls(name: str, env: dict[str, str] | None = None) -> list[str]:
    """URLs for ``name`` under each base URL listed in HEYP_MODELS_MIRROR."""
    raw = (os.environ if env is None else env).get("HEYP_MODELS_MIRROR", "")
    return [f"{base.strip().rstrip('/')}/{name}" for base in raw.split(",") if base.strip()]


def download(url: str, part: str, model: Model) -> None:
    """Stream ``url`` into ``part``, hashing as it goes; raises IntegrityError on any mismatch."""
    h = hashlib.sha256()
    n = 0
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r, open(part, "wb") as f:
        while chunk := r.read(CHUNK):
            n += len(chunk)
            if n > model.size:
                raise IntegrityError(f"larger than the pinned {model.size} bytes")
            h.update(chunk)
            f.write(chunk)
    if n != model.size:
        raise IntegrityError(f"got {n} bytes, expected {model.size}")
    if h.hexdigest() != model.sha256:
        raise IntegrityError(f"sha256 {h.hexdigest()} does not match the pinned {model.sha256}")


def fetch(name: str, model: Model, dest: str, urls: list[str] | None = None) -> str:
    """Place a verified copy of ``name`` in ``dest`` and return its path, or exit non-zero."""
    path = os.path.join(dest, name)
    if verify(path, model):
        return path
    part = path + ".part"
    errors = []
    for url in urls if urls is not None else [*mirror_urls(name), *model.urls]:
        print(f"fetching {name} <- {url}", flush=True)
        try:
            download(url, part, model)
            os.replace(part, path)
            return path
        except Exception as exc:  # try the next source
            errors.append(f"{url}: {exc}")
            print(f"  rejected: {exc}", flush=True)
        finally:
            if os.path.exists(part):
                os.remove(part)
    if os.path.exists(path):  # never leave a stale or tampered file behind
        os.remove(path)
    raise SystemExit(f"could not fetch {name} (sha256 {model.sha256}):\n  " + "\n  ".join(errors or ["no sources"]))


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    dest = args[0] if args else "models"
    os.makedirs(dest, exist_ok=True)
    for name, model in MODELS.items():
        fetch(name, model, dest)
        print(f"{name}: {model.size} bytes sha256={model.sha256} verified", flush=True)


if __name__ == "__main__":
    main()
