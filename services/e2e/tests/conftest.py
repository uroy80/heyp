"""SSRF test bed client: plays the heyp app and reaches the renderer ONLY over the Unix socket.

Run everything with services/e2e/run.sh (needs heyp-renderer:dev, heyp-renderer:test and heyp-egress:dev).
"""
import base64
import io
import json
import os
import time

import httpx
import pytest
from PIL import Image

L3 = os.environ.get("RENDER_DISABLE_L3", "0") != "1"  # tester is told which renderer mode is running
CONTROL = bool(os.environ.get("CONTROL")) or os.environ.get("EGRESS_POLICY", "enforce") == "off"
TARGET_LOGS = {"web": "http://web/log", "secret-app": "http://secret-app/log", "metadata": "http://metadata/log",
               "dns": "http://dns:8080/log", "udp-sink": "http://udp-sink:8080/log"}

renderer = httpx.Client(transport=httpx.HTTPTransport(uds="/run/renderer/r.sock"), base_url="http://renderer",
                        timeout=90)
plain = httpx.Client(timeout=10)


def render(**kw) -> httpx.Response:
    return renderer.post("/v1/render", json=kw)


def info(r: httpx.Response) -> dict:
    return json.loads(base64.b64decode(r.headers["x-render-info"]))


def image(r: httpx.Response) -> Image.Image:
    return Image.open(io.BytesIO(r.content))


def logs(name: str) -> list[dict]:
    return plain.get(TARGET_LOGS[name]).json()


def health() -> dict:
    return renderer.get("/v1/health").json()


def color_fraction(img: Image.Image, rgb, tol=40) -> float:
    img = img.convert("RGB").resize((64, 64))
    b = img.tobytes()
    px = [b[i:i + 3] for i in range(0, len(b), 3)]
    return sum(all(abs(a - b) <= tol for a, b in zip(p, rgb)) for p in px) / len(px)


@pytest.fixture(autouse=True)
def clean_targets():
    for u in TARGET_LOGS.values():
        plain.get(u.replace("/log", "/log/reset"))
    canary_before = len(health().get("canary_hits", []))
    yield
    time.sleep(0.3)  # late sub-resources
    if not CONTROL:
        # Global invariant for EVERY test: internal targets, metadata and the loopback canary never saw a request.
        assert logs("secret-app") == [], "internal service was reached"
        assert logs("metadata") == [], "metadata service was reached"
        assert len(health().get("canary_hits", [])) == canary_before, "renderer loopback was reached"
        assert [h for h in logs("udp-sink") if h["role"] == "udp"] == [], "UDP left the browser"
