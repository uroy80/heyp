"""heyp-renderer unit tests: real Chromium, inline HTML only, no network (``--network none``).

``docker build --target test -t heyp-renderer:test services/renderer &&
docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL heyp-renderer:test``
The SSRF/egress behaviour is covered end to end by services/e2e (renderer + egress + hostile targets).
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
from pathlib import Path

import pytest

os.environ.update({"HEYP_RENDER_EGRESS_SECRET": "unit-test-secret-0123", "HEYP_RENDER_EGRESS_HOST": "egress",
                   "HEYP_RENDER_MAX_CONCURRENT": "1", "HEYP_RENDER_MAX_QUEUE": "1"})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

import renderer  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(renderer.app) as c:
        yield c


def info(r) -> dict:
    return json.loads(base64.b64decode(r.headers["x-render-info"]))


def image(r) -> Image.Image:
    return Image.open(io.BytesIO(r.content))


def color_fraction(img: Image.Image, rgb, tol=40) -> float:
    img = img.convert("RGB").resize((32, 32))
    b = img.tobytes()
    px = [b[i:i + 3] for i in range(0, len(b), 3)]
    return sum(all(abs(a - b) <= tol for a, b in zip(p, rgb)) for p in px) / len(px)
