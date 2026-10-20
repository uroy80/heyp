"""HTTP surface next to the image API: uploads (images and HTML pages; everything else is refused), downloads,
/api/config, /api/tools, the legal pages, the /pdf/* redirect to heypdf, and the security / hosted-mode policies."""
from __future__ import annotations

import io
import json
import logging
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import BaseModel, ConfigDict

from app import main as main_module
from app.config import Settings, VERSION
from app.core import registry
from app.core.registry import Tool
from app.main import CSP, create_app
from app.routers import legal as legal_routes

HOSTED = dict(mode="hosted", operator_name="Ada Operator", operator_email="ops@example.org",
              public_url="https://heyp.example.org")
PDF = b"%PDF-1.7\n1 0 obj << /Type /Catalog >>\nendobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"


def png(size=(64, 48), color=(200, 40, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def gif(frames: int = 3, size=(40, 30)) -> bytes:
    images = [Image.new("RGB", size, (i * 80 % 256, 50, 200 - i * 40 % 200)) for i in range(frames)]
    buf = io.BytesIO()
    images[0].save(buf, "GIF", save_all=True, append_images=images[1:], duration=120, loop=0)
    return buf.getvalue()


@pytest.fixture
def make_client(tmp_path):
    clients = []

    def make(**kw) -> TestClient:
        settings = Settings(**{"tmp_dir": str(tmp_path / f"heyp{len(clients)}"), "max_upload_mb": 30,
                               "max_files": 10, **kw})
        c = TestClient(create_app(settings))
        c.__enter__()
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)
    main_module.set_access_log(False)


@pytest.fixture
def client(make_client):
    return make_client()


def upload(client, *files, accept: str | None = None):
    params = {"accept": accept} if accept else None
    return client.post("/api/files", params=params,
                       files=[("files", (name, data, "application/octet-stream")) for name, data in files])


def upload_one(client, name: str, data: bytes) -> dict:
    res = upload(client, (name, data))
    assert res.status_code == 200, res.text
    assert res.json()["errors"] == [], res.text
    return res.json()["files"][0]


# ------------------------------------------------------------------ uploads
def test_image_fileinfo_has_the_kind_fields(client):
    f = upload_one(client, "a.png", png())
    assert f["kind"] == "image" and f["source"] == "upload" and f["parent_ids"] == [] and f["tool"] is None
    assert f["mime"] == "image/png" and f["width"] == 64 and f["expires_at"] is None
    assert "pages" not in f and "encrypted" not in f
    info = client.get(f"/api/files/{f['id']}").json()
    assert info["kind"] == "image" and "exif" in info


def test_html_pages_are_stored_for_the_capture_tool_not_parsed(client):
    f = upload_one(client, "page.html", b"<!doctype html><p>hi")
    assert {k: f[k] for k in ("kind", "format", "mime")} == {"kind": "html", "format": "html", "mime": "text/html"}
    assert client.get(f"/api/files/{f['id']}").json() == f
    # the image endpoints refuse it clearly
    res = client.get(f"/api/files/{f['id']}/preview")
    assert res.status_code == 415 and res.json()["detail"] == "page.html is an HTML file, not an image"
    res = client.post("/api/process", json={"ids": [f["id"]], "ops": []})
    assert res.status_code == 415 and "page.html is an HTML file" in res.json()["detail"]
    assert client.get(f"/api/files/{f['id']}/faces").status_code == 415


def test_documents_certificates_and_programs_are_refused(client):
    res = upload(client, ("report.pdf", PDF), ("letter.docx", b"PK\x03\x04" + b"\x00" * 64),
                 ("id.p12", b"\x30\x82\x01\x00" + b"\x02" * 300), ("x.exe", b"MZ\x90\x00" + b"\x00" * 64))
    assert res.status_code == 415
    errors = {e["name"]: e["detail"] for e in res.json()["errors"]}
    assert errors == {name: f"“.{name.rsplit('.', 1)[1]}” files can't be opened (unsupported or damaged file)"
                      for name in ("report.pdf", "letter.docx", "id.p12", "x.exe")}
    assert client.app.state.store.ids() == []


def test_accept_restricts_kinds_per_page(client):
    res = upload(client, ("a.html", b"<!doctype html><p>x"), ("b.png", png()), accept="image")
    assert res.status_code == 200
    body = res.json()
    assert [f["kind"] for f in body["files"]] == ["image"]
    assert body["errors"] == [{"name": "a.html", "detail": "This is an HTML file; only images can be opened here"}]
    assert upload(client, ("b.png", png()), accept="html").status_code == 415
    for unknown in ("pdf", "pictures"):
        assert upload(client, ("b.png", png()), accept=unknown).status_code == 422


def test_hosted_upload_byte_budget_is_429(make_client):
    c = make_client(**HOSTED, limit_upload_mb_hour=1)
    assert upload(c, ("a.bin", png((500, 500)))).status_code == 200
    res = upload(c, ("b.png", b"\x89PNG" + bytes(1100 * 1024)))
    assert res.status_code == 429 and int(res.headers["retry-after"]) >= 1
    assert "per hour" in res.json()["detail"]


# ------------------------------------------------------------------ downloads
def test_download_streams_the_bytes_as_an_attachment(client):
    data = png()
    f = upload_one(client, "Urlaub ü.png", data)
    res = client.get(f"/api/files/{f['id']}/download")
    assert res.status_code == 200 and res.content == data
    assert res.headers["content-type"] == "image/png"
    assert res.headers["content-disposition"].startswith("attachment;")
    assert "filename*=UTF-8''Urlaub%20%C3%BC.png" in res.headers["content-disposition"]
    assert res.headers["content-security-policy"] == CSP + "; sandbox"
    assert client.get("/api/files/" + "0" * 32 + "/download").status_code == 404


def test_html_downloads_are_sandboxed_attachments(client):
    page = upload_one(client, "evil.html", b"<!doctype html><script>alert(1)</script>")
    res = client.get(f"/api/files/{page['id']}/download")
    assert res.status_code == 200 and "sandbox" in res.headers["content-security-policy"]
    assert res.headers["content-disposition"].startswith("attachment;")


# ------------------------------------------------------------------ animations through /api/process
def test_animated_gif_keeps_its_frames(client):
    f = upload_one(client, "loop.gif", gif(4))
    assert f["frames"] == 4
    res = client.post("/api/process", json={"ids": [f["id"]], "ops": [{"op": "rotate", "angle": 90}],
                                            "output": {"format": "original"}})
    assert res.status_code == 200, res.text
    with Image.open(io.BytesIO(res.content)) as im:
        assert im.format == "GIF" and im.n_frames == 4 and im.size == (30, 40)
    still = client.post("/api/process", json={"ids": [f["id"]], "ops": [], "output": {"format": "png"}})
    with Image.open(io.BytesIO(still.content)) as im:
        assert getattr(im, "n_frames", 1) == 1


def test_frame_bomb_guard_uses_the_app_settings(make_client):
    c = make_client(max_anim_pixels=4000)  # 4 frames × 40×30 = 4800 px > 4000
    f = upload_one(c, "loop.gif", gif(4))
    res = c.post("/api/process", json={"ids": [f["id"]], "ops": [], "output": {"format": "gif"}})
    assert res.status_code == 413 and "first frame" in res.json()["detail"]
    ok = c.post("/api/process", json={"ids": [f["id"]], "ops": [], "output": {"format": "jpeg"}})
    assert ok.status_code == 200  # a still output only needs the first frame


# ------------------------------------------------------------------ /api/config and /api/health
def test_config_local_is_truthful(client):
    body = client.get("/api/config").json()
    assert body["mode"] == "local" and body["version"] == VERSION and body["sibling_url"] == "http://localhost:8081"
    assert body["ttl_minutes"] == 60 and body["hard_ttl_minutes"] == 0
    assert set(body["features"]) == {"renderer", "upscale", "matting", "plates", "heic", "avif"}
    assert body["limits"]["rate_limits"] is False and body["limits"]["max_upload_mb"] == 30
    assert body["legal"] == {"privacy": "/privacy", "terms": "/terms", "credits": "/credits"}
    assert body["source_url"] == "https://github.com/uroy80/heyp"
    assert not {"copy", "ai", "pdf_url", "heyp_url"} & set(body)


def test_config_hosted(make_client):
    body = make_client(**HOSTED, sibling_url="https://heypdf.example.org").get("/api/config").json()
    assert body["mode"] == "hosted" and body["hard_ttl_minutes"] == 240
    assert body["public_url"] == "https://heyp.example.org" and body["sibling_url"] == "https://heypdf.example.org"
    assert body["operator"] == {"name": "Ada Operator", "email": "ops@example.org"}
    assert body["limits"]["rate_limits"] is True and body["limits"]["jobs_per_minute"] == 30
    text = json.dumps(body).lower()
    assert "local only" not in text and "this machine" not in text


def test_the_sibling_link_can_be_switched_off(make_client):
    assert make_client(sibling_url="").get("/api/config").json()["sibling_url"] is None


def test_request_cap_is_published_only_when_set(make_client):
    """OPS-4: the UIs size upload requests from limits.max_request_mb when the server publishes it (behind a CDN),
    and keep their own CDN-safe default otherwise; the server enforces the same cap before parsing."""
    plain = make_client()
    assert "max_request_mb" not in plain.get("/api/config").json()["limits"]
    assert "max_request_mb" not in plain.get("/api/health").json()["limits"]
    capped = make_client(max_request_mb=31)
    assert capped.get("/api/config").json()["limits"]["max_request_mb"] == 31
    assert capped.get("/api/health").json()["limits"]["max_request_mb"] == 31
    r = capped.post("/api/files", content=b"x", headers={"content-type": "multipart/form-data; boundary=b",
                                                          "content-length": str(31 * 2**20 + 1)})
    assert r.status_code == 413 and "31 MB" in r.json()["detail"]


def test_health_lists_the_features(client):
    features = client.get("/api/health").json()["features"]
    assert features["renderer"] is False
    assert {"heic", "avif", "face", "segmentation", "upscale", "matting", "plates"} <= set(features)
    assert not {"pdf", "docs", "ai"} & set(features)


# ------------------------------------------------------------------ /api/tools
class ListedOpts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = "image"


def _noop(ctx, opts) -> None:
    pass


LISTED_TOOL = Tool(id="img.zz_listed", title="Listed test", summary="x", category="Edit", inputs=("image",),
                   options=ListedOpts, run=_noop, max_files=4, limits={"max_files": 4})


@pytest.fixture
def listed_tool():
    registry.register(LISTED_TOOL)
    yield
    registry.unregister(LISTED_TOOL.id)


def test_tools_listing(client, listed_tool):
    tools = client.get("/api/tools").json()["tools"]
    ids = [t["id"] for t in tools]
    assert {"img.process", "img.make_gif", "img.compress", "img.html_to_image", "img.upscale",
            "img.remove_background", "img.blur", "img.zz_listed"} <= set(ids)
    assert all(t.startswith("img.") for t in ids)
    mine = next(t for t in tools if t["id"] == "img.zz_listed")
    assert mine == {"id": "img.zz_listed", "title": "Listed test", "summary": "x", "category": "Edit",
                    "inputs": ["image"], "min_files": 1, "max_files": 4,
                    "options_schema": ListedOpts.model_json_schema(), "limits": {"max_files": 4}, "lane": "light",
                    "engine": "app", "timeout_s": 120, "outputs": "", "available": True, "reason": ""}
    capture = next(t for t in tools if t["id"] == "img.html_to_image")
    assert capture["available"] is False and capture["reason"]  # no renderer socket in unit tests
    assert client.get("/api/tools/img.zz_listed").json() == mine
    assert client.get("/api/tools/img.missing").status_code == 404
    assert client.post("/api/tools/img.zz_listed/preview", json={"file_ids": []}).status_code == 405


# ------------------------------------------------------------------ legal pages
@pytest.mark.parametrize("page", ["privacy", "terms", "credits"])
def test_legal_pages_render(client, page):
    res = client.get(f"/{page}")
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/html")
    assert legal_routes.PAGES[page] in res.text
    assert res.headers["content-security-policy"] == CSP
    assert client.head(f"/{page}").status_code == 200


def test_legal_page_falls_back_when_the_renderer_fails(make_client, monkeypatch):
    from app import legal

    def broken(*_a, **_k):
        raise RuntimeError("template missing")

    monkeypatch.setattr(legal, "render", broken)
    legal_routes._cache.clear()
    c = make_client(**HOSTED)
    res = c.get("/privacy")
    assert res.status_code == 200 and "Ada Operator" in res.text and "ops@example.org" in res.text
    assert "https://github.com/uroy80/heyp" in res.text
    legal_routes._cache.clear()


def test_hosted_privacy_page_names_the_operator(make_client):
    legal_routes._cache.clear()
    res = make_client(**HOSTED).get("/privacy")
    assert res.status_code == 200 and "Ada Operator" in res.text


# ------------------------------------------------------------------ heypdf moved to its own site
@pytest.mark.parametrize(("path", "target"), [
    ("/pdf", "https://heypdf.example.org/"),
    ("/pdf/", "https://heypdf.example.org/"),
    ("/pdf/merge", "https://heypdf.example.org/merge"),
    ("/pdf/compress/?files=abc&tool=x", "https://heypdf.example.org/compress/?files=abc&tool=x"),
    ("/pdf/a%20b", "https://heypdf.example.org/a%20b"),
])
def test_old_pdf_addresses_move_to_heypdf(make_client, path, target):
    c = make_client(sibling_url="https://heypdf.example.org")
    res = c.get(path, follow_redirects=False)
    assert res.status_code == 308 and res.headers["location"] == target
    assert res.headers["x-content-type-options"] == "nosniff"
    assert c.head(path, follow_redirects=False).status_code == 308


def test_pdf_redirect_defaults_and_switch_off(make_client):
    local = make_client()
    assert local.get("/pdf/merge", follow_redirects=False).headers["location"] == "http://localhost:8081/merge"
    off = make_client(sibling_url="")
    assert off.get("/pdf/merge", follow_redirects=False).status_code == 404
    assert off.get("/pdf/.env").status_code == 404  # dot segments never reach a route


def test_fonts_are_served_with_font_types(client):
    """The slim image has no /etc/mime.types: fonts must not go out as text/plain."""
    from pathlib import Path

    woff2 = next(Path(main_module.STATIC_DIR, "fonts").glob("*.woff2"))
    res = client.get(f"/fonts/{woff2.name}")
    assert res.status_code == 200 and res.headers["content-type"] == "font/woff2"
    res = client.get("/api/assets/fonts/anton.ttf")  # the server-side fonts the editors preview with
    assert res.status_code == 200 and res.headers["content-type"] == "font/ttf"


def test_powerful_browser_features_are_off_everywhere(client):
    for path in ("/", "/pdf/scan", "/api/health", "/privacy"):
        policy = client.get(path, follow_redirects=False).headers["permissions-policy"]
        assert policy == "camera=(), microphone=(), geolocation=(), payment=(), usb=()", path


# ------------------------------------------------------------------ hosted-mode policies
def test_hosted_mode_answers_api_requests_with_no_store(make_client):
    c = make_client(**HOSTED)
    f = upload_one(c, "a.png", png())
    for path in (f"/api/files/{f['id']}", f"/api/files/{f['id']}/preview", f"/api/files/{f['id']}/download",
                 "/api/health", "/api/config"):
        assert c.get(path).headers["cache-control"] == "no-store", path
    assert c.get("/").headers["cache-control"] == "no-cache"


def test_local_mode_keeps_the_v1_preview_caching(client):
    f = upload_one(client, "a.png", png())
    assert client.get(f"/api/files/{f['id']}/preview").headers["cache-control"] == "private, max-age=3600"


def test_hosted_mode_keeps_no_access_log(make_client):
    flt = next(f for f in logging.getLogger("uvicorn.access").filters
               if isinstance(f, main_module.HealthCheckLogFilter))
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("203.0.113.9:5000", "GET", "/api/files", "1.1", 200), None)
    make_client()
    assert flt.filter(record)
    make_client(**HOSTED)
    assert not flt.filter(record)


def test_hard_ttl_applies_in_hosted_mode(make_client):
    c = make_client(**HOSTED)
    f = upload_one(c, "a.png", png())
    assert f["expires_at"] is not None and 0 < f["expires_at"] - time.time() <= 240 * 60
    meta = c.app.state.store.root / f["id"] / "meta.json"
    data = json.loads(meta.read_text())
    data["hard_deadline"] = time.time() - 1
    meta.write_text(json.dumps(data))
    assert c.get(f"/api/files/{f['id']}").status_code == 404  # gone at its hard deadline, however active


def test_html_pages_forbid_cdn_rewriting(client):
    """A CDN must not inject scripts into pages (/privacy says none run): HTML carries no-transform, assets don't."""
    for path in ("/", "/privacy", "/terms"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "no-transform" in r.headers["cache-control"], path
    css = client.get("/styles.css")
    assert "no-transform" not in css.headers.get("cache-control", "")
