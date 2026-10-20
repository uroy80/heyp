"""Download the ONNX models heyp uses into a directory, verifying each against a pinned size + sha256.

Run at image build time (``python fetch_models.py /models``). Every source, including mirrors from
``HEYP_MODELS_MIRROR`` (comma-separated base URLs tried first), is only accepted when the bytes match the
pin; when no source delivers the pinned file the script exits non-zero, so ``docker build`` fails instead
of shipping an image with face detection, segmentation, upscaling or plate detection silently off.

Models we export ourselves (``scripts/export_models/``, reproducible byte for byte) are published as assets
of the ``models-v1`` GitHub release; third-party files are fetched from upstream first, with the release
(which carries a copy of every file below) as the fallback.

    python fetch_models.py DEST                  # required models
    python fetch_models.py DEST --with-optional  # + optional variants (int8 BiRefNet, HEYP_MATTING_MODEL=int8)
    python fetch_models.py DEST --skip a.onnx,b.onnx
    python fetch_models.py DEST --check          # verify what is in DEST, download nothing
    python fetch_models.py --probe               # can every model be downloaded? (HEAD only; CI's first check)
    python fetch_models.py --list                # the selected file names, one per line

The ``models-v1`` release must exist before the first build (CI, Release, ``docker compose up --build``):
``scripts/publish_models.sh`` creates it from verified local copies (deploy/DEPLOY.md §7.1).
"""
from __future__ import annotations

import hashlib
import os
import sys
import urllib.request
from typing import NamedTuple

CHUNK = 1 << 20
TIMEOUT = 120
PROBE_TIMEOUT = 30
RELEASE = "https://github.com/uroy80/heyp/releases/download/models-v1"
RELEASE_HINT = (f"Models heyp exports itself are only published as assets of the models-v1 release ({RELEASE}): "
                "create it with scripts/publish_models.sh, or point HEYP_MODELS_MIRROR at a copy "
                "(deploy/DEPLOY.md §7.1).")


class Model(NamedTuple):
    sha256: str
    size: int
    urls: tuple[str, ...]  # tried in order; pinned to immutable revisions where the host allows it
    licence: str = ""
    source: str = ""       # where the weights come from (for CREDITS.md)
    optional: bool = False  # only fetched with --with-optional


MODELS: dict[str, Model] = {
    "face_detection_yunet_2023mar.onnx": Model(
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        232_589,
        (
            "https://github.com/opencv/opencv_zoo/raw/f12e12798e8314f7c074a6656816c048dcc95b7a"
            "/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
            "https://huggingface.co/opencv/face_detection_yunet/resolve/3cc26e7f1014a5ee5d74a42acee58bafc9d0a310"
            "/face_detection_yunet_2023mar.onnx",
            f"{RELEASE}/face_detection_yunet_2023mar.onnx",
        ),
        "MIT", "OpenCV Zoo face_detection_yunet 2023mar",
    ),
    "u2net_human_seg.onnx": Model(
        "01eb6a29a5c4d8edb30b56adad9bb3a2a0535338e480724a213e0acfd2d1c73c",
        175_997_641,
        (
            "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net_human_seg.onnx",
            "https://huggingface.co/tomjackson2023/rembg/resolve/cd3a3d6767a7859efea31ef0f2f373582cf06d82"
            "/u2net_human_seg.onnx",
            f"{RELEASE}/u2net_human_seg.onnx",
        ),
        "Apache-2.0", "U²-Net human segmentation (xuebinqin/U-2-Net), rembg release v0.0.0",
    ),
    # --- v2: exported by scripts/export_models/ (byte-identical on arm64 and amd64) ---------------------
    "realesr-general-x4v3.onnx": Model(
        "afca20f00823f46845fb34cc55d22861f000e19d13afdc7bd654789a3cda04c7",
        4_866_396,
        (f"{RELEASE}/realesr-general-x4v3.onnx",),
        "BSD-3-Clause", "Real-ESRGAN realesr-general-x4v3.pth (xinntao/Real-ESRGAN v0.2.5.0)",
    ),
    "realesr-general-x4v3-dn05.onnx": Model(
        "3d1b12ecd5392cd846ebebca9e81493a737554ebe59e2675966df2ac33766fcd",
        4_866_396,
        (f"{RELEASE}/realesr-general-x4v3-dn05.onnx",),
        "BSD-3-Clause", "Real-ESRGAN x4v3 blended 50/50 with realesr-general-wdn-x4v3 (v0.2.5.0)",
    ),
    "birefnet-lite-dc-1024.onnx": Model(
        "15daa3c785641d17680cc399df849b6fa54f289410ebd8b73877739899c293f9",
        180_666_098,
        (f"{RELEASE}/birefnet-lite-dc-1024.onnx",),
        "MIT", "BiRefNet_lite (ZhengPeng7/BiRefNet_lite@aa62cd87), native DeformConv export",
    ),
    "birefnet-lite-dc-1024.int8.onnx": Model(
        "e0b9c02ba2aa4397f82328e0b87a7d291252ca5d3b865569ef2e5065e288bef2",
        84_074_805,
        (f"{RELEASE}/birefnet-lite-dc-1024.int8.onnx",),
        "MIT", "birefnet-lite-dc-1024.onnx, dynamic QUInt8 (MatMul+Conv) quantisation", optional=True,
    ),
    # --- v2: third party (upstream release asset first) -------------------------------------------------
    "yolo-v9-t-640-license-plates-end2end.onnx": Model(
        "c3c1026ca7d0585dd88084d68182dd897113712fa734ae1557ca70174440c076",
        7_835_770,
        (
            "https://github.com/ankandrew/open-image-models/releases/download/assets"
            "/yolo-v9-t-640-license-plates-end2end.onnx",
            f"{RELEASE}/yolo-v9-t-640-license-plates-end2end.onnx",
        ),
        "MIT (YOLOv9 lineage: GPL-3.0)", "open-image-models (ankandrew) YOLOv9-t-640 plate detector, release 'assets'",
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


def sources(name: str, model: Model, env: dict[str, str] | None = None) -> list[str]:
    """Where ``name`` is looked for, in order: HEYP_MODELS_MIRROR bases, then the model's own URLs."""
    return [*mirror_urls(name, env), *model.urls]


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
    tried = urls if urls is not None else sources(name, model)
    for url in tried:
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
    hint = f"\n{RELEASE_HINT}" if any(u.startswith(RELEASE + "/") for u in tried) else ""
    raise SystemExit(f"could not fetch {name} (sha256 {model.sha256}):\n  "
                     + "\n  ".join(errors or ["no sources"]) + hint)


def probe(url: str, model: Model) -> None:
    """HEAD ``url`` (following redirects); raises unless it answers with the pinned size, when it states one."""
    with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=PROBE_TIMEOUT) as r:
        length = r.headers.get("Content-Length")
    if length is not None and int(length) != model.size:
        raise IntegrityError(f"{length} bytes, expected {model.size}")


def probe_all(models: dict[str, Model], env: dict[str, str] | None = None) -> list[str]:
    """Names in ``models`` that no source offers (each model stops at its first good source)."""
    missing = []
    for name, model in models.items():
        errors = []
        for url in sources(name, model, env):
            try:
                probe(url, model)
            except Exception as exc:
                errors.append(f"{url}: {exc}")
                continue
            print(f"{name}: ok <- {url}", flush=True)
            break
        else:
            missing.append(name)
            print(f"{name}: NO SOURCE\n  " + "\n  ".join(errors or ["no sources"]), flush=True)
    return missing


def selected(with_optional: bool = False, skip: set[str] | None = None) -> dict[str, Model]:
    """The models to fetch: required ones (+ optional ones on request) minus ``skip``."""
    unknown = (skip or set()) - set(MODELS)
    if unknown:
        raise SystemExit(f"unknown model(s) in --skip: {', '.join(sorted(unknown))}")
    return {name: m for name, m in MODELS.items()
            if (with_optional or not m.optional) and name not in (skip or set())}


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    with_optional = "--with-optional" in args
    check_only = "--check" in args
    probe_only = "--probe" in args
    list_only = "--list" in args
    skip: set[str] = set()
    if "--skip" in args:
        i = args.index("--skip")
        if i + 1 >= len(args):
            raise SystemExit("--skip needs a comma-separated list of model file names")
        skip = {n.strip() for n in args[i + 1].split(",") if n.strip()}
        del args[i:i + 2]
    positional = [a for a in args if not a.startswith("--")]
    dest = positional[0] if positional else "models"
    models = selected(with_optional, skip)
    if list_only:
        print("\n".join(models), flush=True)
        return
    if probe_only:
        missing = probe_all(models)
        if missing:
            msg = f"{len(missing)} model(s) cannot be downloaded from any source ({', '.join(missing)}). {RELEASE_HINT}"
            if os.environ.get("GITHUB_ACTIONS") == "true":
                print(f"::error title=Model sources::{msg}", flush=True)
            raise SystemExit(msg)
        return
    if check_only:
        bad = [name for name, model in models.items() if not verify(os.path.join(dest, name), model)]
        for name in models:
            print(f"{name}: {'MISSING OR MODIFIED' if name in bad else 'ok'}", flush=True)
        if bad:
            raise SystemExit(f"{len(bad)} model(s) missing or not matching their pin in {dest}")
        return
    os.makedirs(dest, exist_ok=True)
    for name, model in models.items():
        fetch(name, model, dest)
        print(f"{name}: {model.size} bytes sha256={model.sha256} verified", flush=True)


if __name__ == "__main__":
    main()
