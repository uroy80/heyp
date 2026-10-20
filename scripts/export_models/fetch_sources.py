"""Download the pinned upstream weights and model code the exports start from (size + sha256 checked).

usage: python -I fetch_sources.py DEST

DEST receives realesr-general-x4v3.pth, realesr-general-wdn-x4v3.pth, birefnet_lite.safetensors and the
BiRefNet_lite model code as a package (DEST/birefnet_pkg/). Anything that does not match its pin is
deleted and the script exits non-zero.
"""
from __future__ import annotations

import hashlib
import os
import sys
import urllib.request

ESRGAN = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0"
BIREFNET = "https://huggingface.co/ZhengPeng7/BiRefNet_lite/resolve/aa62cd87eafb9cc43056d08ef3615a14628b831d"

# name in DEST: (url, size, sha256) — Real-ESRGAN: BSD-3-Clause; BiRefNet_lite: MIT
SOURCES = {
    "realesr-general-x4v3.pth": (f"{ESRGAN}/realesr-general-x4v3.pth", 4_885_111,
                                 "8dc7edb9ac80ccdc30c3a5dca6616509367f05fbc184ad95b731f05bece96292"),
    "realesr-general-wdn-x4v3.pth": (f"{ESRGAN}/realesr-general-wdn-x4v3.pth", 4_885_111,
                                     "1641f8c4464b9f097c9fdda5589273713f67cf59f3d909e0bd688f0cee269dca"),
    "birefnet_lite.safetensors": (f"{BIREFNET}/model.safetensors", 177_634_392,
                                  "4417d89795250e698c3cb0ae8df15743810065f646f48a694fdfa7ca052d0815"),
    "birefnet_pkg/birefnet.py": (f"{BIREFNET}/birefnet.py", 92_134,
                                 "af8568b5be406bf4d2a68a7ed6d72e40f73b37a1fb6fc9ebd71b5b3cbcd069c9"),
    "birefnet_pkg/BiRefNet_config.py": (f"{BIREFNET}/BiRefNet_config.py", 298,
                                        "e7b8c2a74f6cea6a59553d517f71d47f2c1d90e670a13416af17c25fe2f3dc52"),
}


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def fetch(dest: str, name: str, url: str, size: int, sha: str) -> None:
    path = os.path.join(dest, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.isfile(path) and os.path.getsize(path) == size and sha256_of(path) == sha:
        print(f"{name}: present, verified")
        return
    part = path + ".part"
    print(f"{name} <- {url}", flush=True)
    try:
        with urllib.request.urlopen(url, timeout=300) as r, open(part, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        got = os.path.getsize(part), sha256_of(part)
        if got != (size, sha):
            raise SystemExit(f"{name}: got {got[0]} bytes sha256 {got[1]}, expected {size} bytes {sha}")
        os.replace(part, path)
    finally:
        if os.path.exists(part):
            os.remove(part)


def main() -> None:
    dest = sys.argv[1] if len(sys.argv) > 1 else "sources"
    for name, (url, size, sha) in SOURCES.items():
        fetch(dest, name, url, size, sha)
    open(os.path.join(dest, "birefnet_pkg", "__init__.py"), "w").close()  # birefnet.py uses a relative import
    print("sources verified")


if __name__ == "__main__":
    main()
