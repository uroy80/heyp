"""Real renders of inline HTML through the HTTP API (Chromium in this image; no network at all)."""
from __future__ import annotations


import pytest
from conftest import color_fraction, image, info

import renderer

GREEN = "<body style='margin:0;background:#0a0'><h1 style='color:#fff'>heyp</h1></body>"


def render(client, **kw):
    return client.post("/v1/render", json=kw)


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    h = client.get("/v1/health").json()
    assert h["ok"] is True and h["version"] and h["slots"] == 1


def test_png(client):
    r = render(client, html=GREEN)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "no-store"
    img = image(r)
    assert img.size[0] == 1366 and color_fraction(img, (0, 170, 0)) > 0.8
    i = info(r)
    assert i["final_url"] == "about:blank" and i["truncated"] is False and i["viewport_width"] == 1366


def test_jpeg(client):
    r = render(client, html=GREEN, output="jpeg", jpeg_quality=60, viewport_width=800, full_page=False)
    assert r.headers["content-type"] == "image/jpeg" and r.content[:3] == b"\xff\xd8\xff"
    assert image(r).size == (800, 768)


@pytest.mark.parametrize("max_height,expected", [(20000, 20000), (5000, 5000)])
def test_full_page_height_is_capped(client, max_height, expected):
    tall = "<body style='margin:0'><div style='height:60000px;background:linear-gradient(#f00,#00f)'></div></body>"
    r = render(client, html=tall, max_height=max_height)
    assert r.status_code == 200 and image(r).size == (1366, expected) and info(r)["truncated"] is True


def test_pixel_cap_on_hidpi_device(client):
    tall = "<body style='margin:0'><div style='height:60000px;background:#0a0'></div></body>"
    r = render(client, html=tall, device="mobile-iphone")
    w, h = image(r).size
    assert w == 393 * 3 and w * h <= renderer.MAX_PIXELS


def test_javascript_off_still_renders(client):
    page = "<body style='margin:0;background:#0a0'><script>document.body.style.background='#f00'</script></body>"
    on, off = render(client, html=page), render(client, html=page, javascript=False)
    assert color_fraction(image(on), (255, 0, 0)) > 0.8 and color_fraction(image(off), (0, 170, 0)) > 0.8


def test_webrtc_and_popups_are_gone(client):
    page = ("<body style='margin:0;background:#f00'><script>"
            "const ok = typeof RTCPeerConnection === 'undefined' && window.open('https://example.com') === null;"
            "if (ok) document.body.style.background = '#0a0';</script></body>")
    assert color_fraction(image(render(client, html=page)), (0, 170, 0)) > 0.8


def test_cookie_banners_hidden(client):
    page = ("<body style='margin:0;background:#fff'><h1>content</h1><div id='onetrust-banner-sdk' "
            "style='position:fixed;inset:0;background:#f00'>We value your privacy</div></body>")
    on = image(render(client, html=page, full_page=False))
    off = image(render(client, html=page, full_page=False, hide_cookie_banners=False))
    assert color_fraction(off, (255, 0, 0)) > 0.5 and color_fraction(on, (255, 0, 0)) < 0.05


def test_dark_scheme(client):
    page = ("<style>body{margin:0;background:#fff}@media (prefers-color-scheme: dark){body{background:#000}}</style>"
            "<body></body>")
    assert color_fraction(image(render(client, html=page, color_scheme="dark", full_page=False)), (0, 0, 0)) > 0.9


def test_subresources_to_internal_targets_do_not_break_the_render(client):
    page = ("<img src='http://169.254.169.254/latest/meta-data/'><img src='http://127.0.0.1:9/x'>"
            "<iframe src='file:///etc/passwd'></iframe><h1>still here</h1>")
    r = render(client, html=page)
    assert r.status_code == 200 and info(r)["final_url"] == "about:blank"


def test_url_preflight_errors(client):
    r = render(client, url="file:///etc/passwd")
    assert r.status_code == 400 and r.json() == {"error": "invalid_url",
                                                 "detail": "only http and https URLs can be rendered"}
    r = render(client, url="http://169.254.169.254/")
    assert r.status_code == 422 and r.json()["error"] == "blocked_target"


def test_unreachable_egress_is_a_clean_502(client):
    """No network here: the page cannot load; the answer names the Chromium error only, never the URL."""
    r = render(client, url="https://www.example.com/private?token=s3cret", timeout_ms=8000)
    assert r.status_code in (502, 504), r.text
    assert "s3cret" not in r.text and "example.com" not in r.text


def test_bad_requests_are_400(client):
    r = client.post("/v1/render", json={"url": "https://example.com", "html": "<p>x</p>"})
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    r = client.post("/v1/render", json={"html": "<p>x</p>", "evil": True})
    assert r.status_code == 400 and "evil" in r.json()["detail"]


def test_output_size_cap(client, monkeypatch):
    monkeypatch.setattr(renderer, "MAX_OUTPUT_BYTES", 1000)
    r = render(client, html=GREEN)
    assert r.status_code == 413 and r.json()["error"] == "too_large"


def test_full_queue_is_503_with_retry_after(client, monkeypatch):
    monkeypatch.setattr(renderer.R, "waiting", renderer.MAX_CONCURRENT + renderer.MAX_QUEUE)
    r = render(client, html=GREEN)
    assert r.status_code == 503 and r.headers["retry-after"] == "5" and r.json()["error"] == "busy"


def test_renders_keep_working_after_errors(client):
    render(client, url="http://127.0.0.1/")
    assert render(client, html=GREEN).status_code == 200
