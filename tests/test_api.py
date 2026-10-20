"""HTTP API (PLAN.md §3) through FastAPI's TestClient."""
from __future__ import annotations

import base64
import threading
import json
import os
import re
import time
import zipfile
from io import BytesIO
from urllib.parse import unquote

import numpy as np
import pytest
from PIL import Image

from fastapi.testclient import TestClient

from app.api import content_disposition, dedupe_name, output_name
from app.config import VERSION, Settings
from app.imaging import io as imgio, matting, segment
from app.main import CSP, create_app

ID_RE = re.compile(r"^[0-9a-f]{32}$")
PREVIEW_KEYS = {"width", "height", "format", "size_bytes", "dpi", "warnings", "image", "encode", "passport"}


# ------------------------------------------------------------------ helpers / fixtures
def encode(img: Image.Image, fmt: str = "JPEG", **kw) -> bytes:
    buf = BytesIO()
    img.save(buf, format=fmt, **kw)
    return buf.getvalue()


def photo(w: int = 320, h: int = 240, seed: int = 0) -> Image.Image:
    """A textured RGB image (gradients + noise) so encoders behave like on photos."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w]
    arr = np.stack([x * 255 // max(1, w - 1), y * 255 // max(1, h - 1), (x + y) % 256], axis=-1).astype(np.int16)
    arr += rng.integers(-12, 12, arr.shape, dtype=np.int16)
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")


def heic_bytes(img: Image.Image) -> bytes:
    import pillow_heif

    pillow_heif.register_heif_opener()
    return encode(img, "HEIF", quality=90)


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(tmp_dir=str(tmp_path / "heyp"), max_upload_mb=5, max_files=10, max_jobs=2,
                    cache_items=4, preview_max=800, ttl_minutes=60)


@pytest.fixture()
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def upload(client, *files: tuple[str, bytes], mime: str = "application/octet-stream"):
    return client.post("/api/files", files=[("files", (name, data, mime)) for name, data in files])


def upload_ok(client, *files: tuple[str, bytes]) -> list[dict]:
    res = upload(client, *files)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["errors"] == []
    return body["files"]


def process(client, ids, ops=None, **output):
    return client.post("/api/process", json={"ids": ids, "ops": ops or [], "output": output})


# ------------------------------------------------------------------ static endpoints
def test_health(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok" and body["version"] == VERSION
    assert set(body["features"]) == {"heic", "avif", "face", "segmentation", "upscale", "matting", "plates",
                                     "renderer"}
    assert all(isinstance(v, bool) for v in body["features"].values())
    assert body["features"]["heic"] is True
    from app.imaging import upscale

    assert body["limits"] == {"max_upload_mb": 5, "max_files": 10,
                              "max_pixels": min(50_000_000, imgio.max_pixels()),  # the stricter limit
                              "upscale_max_input_pixels": upscale.max_input_pixels()}  # the UI warns before a job


def test_health_feature_import_error_is_false(client, monkeypatch):
    import app.api as api_module

    monkeypatch.setattr(api_module, "_feature", lambda name: False)
    assert client.get("/api/health").json()["features"]["segmentation"] is False


def test_formats(client):
    body = client.get("/api/formats").json()
    assert {k: body[k] for k in ("input", "output")} == {  # decoder and encoder lists
        "input": ["heic", "heif", "jpeg", "png", "webp", "avif", "tiff", "bmp", "gif", "ico", "jp2"],
        "output": ["jpeg", "png", "webp", "avif", "heic", "tiff", "bmp", "gif", "pdf"],
    }
    assert {"psd", "svg", "dng"} <= set(body["images"]) and body["html"] == ["html", "htm"]
    assert set(body) == {"input", "output", "images", "html"}


def test_passport_presets(client):
    body = client.get("/api/presets/passport").json()
    assert {"presets", "papers"} == set(body)
    assert any(p["id"] == "us" for p in body["presets"])
    paper = next(p for p in body["papers"] if p["id"] == "4x6")
    assert paper["width_mm"] == 101.6 and paper["height_mm"] == 152.4 and paper["name"]


def test_security_headers(client):
    res = client.get("/api/health")
    assert res.headers["content-security-policy"] == CSP
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["referrer-policy"] == "no-referrer"


def test_static_ui_served_with_revalidation(client):
    index = client.get("/")
    assert index.status_code == 200 and index.headers["content-type"].startswith("text/html")
    assert index.headers["cache-control"] == "no-cache, no-transform"
    assert index.headers["content-security-policy"] == CSP
    script = client.get("/app.js")
    assert script.status_code == 200 and "javascript" in script.headers["content-type"]
    assert script.headers["cache-control"] == "no-cache"
    assert "cache-control" not in client.get("/api/health").headers


# ------------------------------------------------------------------ upload
def test_upload_jpeg_png_heic(client):
    files = upload_ok(client, ("a.jpg", encode(photo(320, 240))),
                      ("b.png", encode(photo(100, 50), "PNG")),
                      ("IMG_0001.HEIC", heic_bytes(photo(160, 120))))
    assert [f["name"] for f in files] == ["a.jpg", "b.png", "IMG_0001.HEIC"]
    assert [f["format"] for f in files] == ["jpeg", "png", "heic"]
    assert [(f["width"], f["height"]) for f in files] == [(320, 240), (100, 50), (160, 120)]
    for f in files:
        assert ID_RE.fullmatch(f["id"])
        assert f["size_bytes"] > 0 and "exif" not in f


def test_upload_applies_exif_orientation(client):
    exif = Image.Exif()
    exif[0x0112] = 6
    (f,) = upload_ok(client, ("rot.jpg", encode(photo(200, 100), exif=exif.tobytes())))
    assert (f["width"], f["height"]) == (100, 200)
    assert f["orientation"] == 6


def test_upload_partial_failure_reports_errors(client):
    # anything that is neither an image nor an HTML page is refused, the rest of the request still succeeds
    res = upload(client, ("good.png", encode(photo(), "PNG")), ("notes.txt", b"hello, not an image"))
    assert res.status_code == 200
    body = res.json()
    assert len(body["files"]) == 1
    assert body["errors"] == [{"name": "notes.txt",
                               "detail": "“.txt” files can't be opened (unsupported or damaged file)"}]
    assert len(client.app.state.store.ids()) == 1  # the bad upload was cleaned up


def test_upload_all_bad_is_415(client):
    res = upload(client, ("x.jpg", b"\xff\xd8\xff garbage"), ("y.heic", b"nope"))
    assert res.status_code == 415
    body = res.json()
    assert body["detail"] and body["files"] == [] and len(body["errors"]) == 2
    assert client.app.state.store.ids() == []


def test_upload_oversize_is_413(tmp_path):
    settings = Settings(tmp_dir=str(tmp_path / "s"), max_upload_mb=1, max_files=5)
    with TestClient(create_app(settings)) as c:
        big = os.urandom(1024 * 1024 + 10)
        res = upload(c, ("huge.jpg", big))
        assert res.status_code == 413
        assert "limit" in res.json()["detail"]
        assert res.json()["errors"][0]["name"] == "huge.jpg"
        assert c.app.state.store.ids() == []
        ok = upload(c, ("huge.jpg", big), ("small.png", encode(photo(), "PNG")))
        assert ok.status_code == 200 and len(ok.json()["files"]) == 1 and len(ok.json()["errors"]) == 1


def test_upload_empty_file_is_400(client):
    res = upload(client, ("empty.jpg", b""))
    assert res.status_code == 400


def test_upload_too_many_files(client):
    res = upload(client, *[(f"{i}.png", b"x") for i in range(11)])
    assert res.status_code == 413
    assert "Too many files" in res.json()["detail"]


def test_upload_requires_files_field(client):
    res = client.post("/api/files", data={"x": "1"})
    assert res.status_code == 422
    assert isinstance(res.json()["detail"], str)


def test_upload_name_is_sanitised(client):
    (f,) = upload_ok(client, ("../../etc/passwd.png", encode(photo(), "PNG")))
    assert f["name"] == "passwd.png"


# ------------------------------------------------------------------ file endpoints
def test_get_info_preview_and_delete(client):
    (f,) = upload_ok(client, ("a.jpg", encode(photo(400, 300), exif=Image.Exif().tobytes())))
    info = client.get(f"/api/files/{f['id']}")
    assert info.status_code == 200
    assert info.json()["id"] == f["id"] and "exif" in info.json()

    thumb = client.get(f"/api/files/{f['id']}/preview", params={"max": 100})
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/jpeg"
    assert thumb.headers["cache-control"] == "private, max-age=3600"
    with Image.open(BytesIO(thumb.content)) as im:
        assert max(im.size) <= 100
    again = client.get(f"/api/files/{f['id']}/preview", params={"max": 100})
    assert again.content == thumb.content

    big = client.get(f"/api/files/{f['id']}/preview", params={"max": 5000})
    with Image.open(BytesIO(big.content)) as im:
        assert max(im.size) <= 800  # clamped to HEYP_PREVIEW_MAX

    assert client.delete(f"/api/files/{f['id']}").status_code == 204
    assert client.get(f"/api/files/{f['id']}").status_code == 404
    assert client.get(f"/api/files/{f['id']}/preview").status_code == 404
    assert client.delete(f"/api/files/{f['id']}").status_code == 404
    assert process(client, [f["id"]]).status_code == 404


def test_preview_png_for_alpha(client):
    img = Image.new("RGBA", (64, 64), (255, 0, 0, 0))
    (f,) = upload_ok(client, ("t.png", encode(img, "PNG")))
    res = client.get(f"/api/files/{f['id']}/preview")
    assert res.headers["content-type"] == "image/png"


@pytest.mark.parametrize("bad", ["nothex", "g" * 32, "A" * 32, "0" * 33, "0" * 31 + "%0A"])
def test_invalid_path_ids_are_404(client, bad):
    assert client.get(f"/api/files/{bad}").status_code == 404
    assert client.delete(f"/api/files/{bad}").status_code == 404


def test_unknown_and_expired_ids(client):
    assert client.get("/api/files/" + "0" * 32).status_code == 404
    (f,) = upload_ok(client, ("a.png", encode(photo(), "PNG")))
    meta = client.app.state.store.root / f["id"] / "meta.json"
    old = time.time() - 3 * 3600
    os.utime(meta, (old, old))
    assert client.get(f"/api/files/{f['id']}").status_code == 404


def test_faces_endpoint(client):
    (f,) = upload_ok(client, ("a.png", encode(photo(200, 150), "PNG")))
    body = client.get(f"/api/files/{f['id']}/faces").json()
    assert body["width"] == 200 and body["height"] == 150 and isinstance(body["faces"], list)


def test_startup_wipes_previous_uploads(tmp_path):
    root = tmp_path / "store"
    stale = root / ("a" * 32)
    stale.mkdir(parents=True)
    (stale / "source").write_bytes(b"old")
    with TestClient(create_app(Settings(tmp_dir=str(root)))):
        assert not stale.exists()


# ------------------------------------------------------------------ preview
def test_preview_json_shape_and_exact_size(client):
    (f,) = upload_ok(client, ("a.jpg", encode(photo(1000, 500))))
    body = {"ids": [f["id"]], "ops": [{"op": "resize", "mode": "fit", "width": 600}],
            "output": {"format": "jpeg", "quality": 80}}
    res = client.post("/api/preview", json=body)
    assert res.status_code == 200, res.text
    data = res.json()
    assert set(data) == PREVIEW_KEYS
    assert (data["width"], data["height"], data["format"]) == (600, 300, "jpeg")
    assert data["image"].startswith("data:image/jpeg;base64,")
    shown = base64.b64decode(data["image"].split(",", 1)[1])
    with Image.open(BytesIO(shown)) as im:
        assert im.size == (600, 300)
    full = client.post("/api/process", json=body)
    assert data["size_bytes"] == len(full.content)


def test_preview_display_is_downscaled(client):
    (f,) = upload_ok(client, ("a.png", encode(photo(1000, 500), "PNG")))
    res = client.post("/api/preview", params={"max": 64}, json={"ids": [f["id"]], "output": {"format": "png"}})
    data = res.json()
    assert (data["width"], data["height"]) == (1000, 500)
    with Image.open(BytesIO(base64.b64decode(data["image"].split(",", 1)[1]))) as im:
        assert max(im.size) <= 64


def test_preview_alpha_is_png_and_pdf_works(client):
    (f,) = upload_ok(client, ("t.png", encode(Image.new("RGBA", (50, 40), (0, 0, 255, 100)), "PNG")))
    data = client.post("/api/preview", json={"ids": [f["id"]], "output": {"format": "png"}}).json()
    assert data["image"].startswith("data:image/png;base64,")
    pdf = client.post("/api/preview", json={"ids": [f["id"]], "output": {"format": "pdf"}}).json()
    assert pdf["format"] == "pdf" and pdf["image"].startswith("data:image/")


def test_preview_requires_exactly_one_id(client):
    files = upload_ok(client, ("a.png", encode(photo(), "PNG")), ("b.png", encode(photo(), "PNG")))
    res = client.post("/api/preview", json={"ids": [f["id"] for f in files]})
    assert res.status_code == 422


# ------------------------------------------------------------------ process
def test_process_single_heic_to_jpeg(client):
    (f,) = upload_ok(client, ("IMG_0001.HEIC", heic_bytes(photo(240, 160))))
    res = process(client, [f["id"]], format="jpeg", quality=85)
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "image/jpeg"
    assert res.headers["x-heyp-width"] == "240" and res.headers["x-heyp-height"] == "160"
    assert json.loads(unquote(res.headers["x-heyp-warnings"])) == []
    cd = res.headers["content-disposition"]
    assert cd.startswith("attachment;") and 'filename="IMG_0001.jpg"' in cd and "filename*=UTF-8''IMG_0001.jpg" in cd
    with Image.open(BytesIO(res.content)) as im:
        assert im.format == "JPEG" and im.size == (240, 160)


def test_process_rotate_then_crop(client):
    (f,) = upload_ok(client, ("a.png", encode(photo(300, 200), "PNG")))
    res = process(client, [f["id"]], [{"op": "rotate", "angle": 90},
                                      {"op": "crop", "x": 0, "y": 0, "width": 200, "height": 100}], format="png")
    assert (res.headers["x-heyp-width"], res.headers["x-heyp-height"]) == ("200", "100")
    res2 = process(client, [f["id"]], [{"op": "crop", "x": 0, "y": 0, "width": 200, "height": 100},
                                       {"op": "rotate", "angle": 90}], format="png")
    assert (res2.headers["x-heyp-width"], res2.headers["x-heyp-height"]) == ("100", "200")


def test_process_unicode_filename_and_override(client):
    (f,) = upload_ok(client, ("été 写真.png", encode(photo(), "PNG")))
    cd = process(client, [f["id"]], format="webp").headers["content-disposition"]
    assert "filename*=UTF-8''%C3%A9t%C3%A9%20%E5%86%99%E7%9C%9F.webp" in cd
    assert re.search(r'filename="[\x20-\x7e]+\.webp"', cd)
    cd2 = process(client, [f["id"]], format="png", filename="../my photo").headers["content-disposition"]
    assert 'filename="my photo.png"' in cd2


def test_process_target_size(client):
    (f,) = upload_ok(client, ("a.png", encode(photo(900, 900, seed=3), "PNG")))
    res = process(client, [f["id"]], format="jpeg", max_kb=30)
    assert res.status_code == 200 and len(res.content) <= 30 * 1024


def test_process_many_to_zip_with_deduped_names(client):
    files = upload_ok(client, ("a.jpg", encode(photo(seed=1))), ("a.jpg", encode(photo(seed=2))),
                      ("b.heic", heic_bytes(photo(64, 48))))
    res = process(client, [f["id"] for f in files], [{"op": "resize", "mode": "long_edge", "size": 100}],
                  format="png")
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "application/zip"
    assert 'filename="heyp-3-files.zip"' in res.headers["content-disposition"]
    assert "x-heyp-warnings" not in res.headers
    with zipfile.ZipFile(BytesIO(res.content)) as zf:
        assert zf.namelist() == ["a.png", "a (2).png", "b.png"]
        assert all(i.compress_type == zipfile.ZIP_STORED for i in zf.infolist())
        for name in zf.namelist():
            with Image.open(BytesIO(zf.read(name))) as im:
                assert im.format == "PNG" and max(im.size) == 100


def test_process_many_to_one_pdf(client):
    files = upload_ok(client, ("a.jpg", encode(photo(seed=1))), ("b.png", encode(photo(seed=2), "PNG")))
    res = process(client, [f["id"] for f in files], format="pdf", combine_pdf=True)
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "application/pdf"
    assert res.content.startswith(b"%PDF")
    assert len(re.findall(rb"/Type\s*/Page\b", res.content)) == 2
    assert 'filename="heyp-2-files.pdf"' in res.headers["content-disposition"]


def test_process_many_pdf_without_combine_is_zip(client):
    files = upload_ok(client, ("a.jpg", encode(photo(seed=1))), ("b.png", encode(photo(seed=2), "PNG")))
    res = process(client, [f["id"] for f in files], format="pdf", combine_pdf=False)
    with zipfile.ZipFile(BytesIO(res.content)) as zf:
        assert zf.namelist() == ["a.pdf", "b.pdf"]


def test_process_invalid_requests(client):
    (f,) = upload_ok(client, ("a.png", encode(photo(), "PNG")))
    res = process(client, [f["id"]], [{"op": "rotate", "angle": 999}])
    assert res.status_code == 422 and isinstance(res.json()["detail"], str) and "angle" in res.json()["detail"]
    assert process(client, [f["id"]], [{"op": "teleport"}]).status_code == 422
    assert process(client, ["../../etc/passwd"]).status_code == 422
    assert process(client, ["0" * 32]).status_code == 404
    assert process(client, [f["id"], "0" * 32]).status_code == 404
    assert client.post("/api/process", json={"ids": [f["id"]], "output": {"format": "jxl"}}).status_code == 422
    broken = client.post("/api/process", content=b"{not json", headers={"content-type": "application/json"})
    assert broken.status_code == 422
    res = process(client, [f["id"]], [{"op": "passport", "preset": "atlantis"}])
    assert res.status_code == 422 and "atlantis" in res.json()["detail"]


def test_passport_and_sheet_through_api(client):
    (f,) = upload_ok(client, ("me.jpg", encode(photo(400, 500))))
    res = process(client, [f["id"]], [{"op": "passport", "preset": "us", "replace_background": False}],
                  format="jpeg")
    assert res.status_code == 200, res.text
    assert (res.headers["x-heyp-width"], res.headers["x-heyp-height"]) == ("600", "600")
    assert 'filename="me_passport-us.jpg"' in res.headers["content-disposition"]
    assert json.loads(unquote(res.headers["x-heyp-warnings"]))  # synthetic image → "no face" warning
    with Image.open(BytesIO(res.content)) as im:
        assert round(im.info["dpi"][0]) == 300

    res = process(client, [f["id"]], [{"op": "passport", "preset": "us", "replace_background": False},
                                      {"op": "sheet", "paper": "4x6"}], format="jpeg")
    assert res.status_code == 200, res.text
    assert sorted((int(res.headers["x-heyp-width"]), int(res.headers["x-heyp-height"]))) == [1200, 1800]
    assert 'filename="me_passport-us_sheet-4x6.jpg"' in res.headers["content-disposition"]
    preview = client.post("/api/preview", json={"ids": [f["id"]], "ops": [
        {"op": "passport", "preset": "us", "replace_background": False}, {"op": "sheet", "paper": "4x6"}]}).json()
    assert preview["dpi"] == [300, 300]


def test_background_without_segmentation_is_503(client, monkeypatch):
    monkeypatch.setattr(segment, "available", lambda: False)
    monkeypatch.setattr(matting, "available", lambda: False)  # v2: neither background model installed
    (f,) = upload_ok(client, ("a.png", encode(photo(), "PNG")))
    res = process(client, [f["id"]], [{"op": "background", "mode": "replace", "color": "#ffffff"}])
    assert res.status_code == 503 and "unavailable" in res.json()["detail"]


def test_too_many_pixels_is_413(tmp_path):
    """API-14: the app's own Settings drive the pixel limit (not the process environment)."""
    with TestClient(create_app(Settings(tmp_dir=str(tmp_path / "s"), max_pixels=1_000_000))) as c:
        (f,) = upload_ok(c, ("a.png", encode(photo(200, 200), "PNG")))
        res = process(c, [f["id"]], [{"op": "resize", "mode": "scale", "scale": 1000}])
        assert res.status_code == 413 and "1 MP limit" in res.json()["detail"]
        res = upload(c, ("big.png", encode(photo(1200, 1000), "PNG")))
        assert res.status_code == 413 and "above the 1 MP limit" in res.json()["detail"]
        assert len(c.app.state.store.ids()) == 1  # the refused upload is not kept
        assert c.get("/api/health").json()["limits"]["max_pixels"] == 1_000_000


# ------------------------------------------------------------------ naming helpers
def test_output_names():
    assert output_name("IMG_0001.HEIC", "jpeg", {}) == "IMG_0001.jpg"
    assert output_name("IMG_0001.HEIC", "jpeg", {"passport_preset": "us"}) == "IMG_0001_passport-us.jpg"
    assert output_name("scan.png", "pdf", {"sheet_paper": "a4"}) == "scan_sheet-a4.pdf"
    assert output_name("x.png", "png", {"passport_preset": "us"}, "custom") == "custom.png"
    assert output_name("noext", "webp", {}) == "noext.webp"
    assert output_name("a/b\\c.tiff", "tiff", {}) == f"c.{imgio.ext_for('tiff')}"


def test_dedupe_and_disposition():
    used: set[str] = set()
    assert [dedupe_name(n, used) for n in ["x.jpg", "x.jpg", "X.JPG", "y"]] == ["x.jpg", "x (2).jpg", "X (3).JPG", "y"]
    cd = content_disposition("résumé \"photo\".jpg")
    assert cd == ("attachment; filename=\"resume _photo_.jpg\"; "
                  "filename*=UTF-8''r%C3%A9sum%C3%A9%20%22photo%22.jpg")
    assert content_disposition("写真.jpg").startswith('attachment; filename="heyp.jpg";')
    assert content_disposition("x/y;z.png").startswith('attachment; filename="x_y_z.png";')


# ------------------------------------------------------------------ v1.1: per-file ops (C1)
def test_ops_by_id_in_zip(client):
    a, b = upload_ok(client, ("a.png", encode(photo(320, 240), "PNG")), ("b.png", encode(photo(320, 240), "PNG")))
    res = client.post("/api/process", json={
        "ids": [a["id"], b["id"]], "ops": [{"op": "resize", "mode": "long_edge", "size": 160}],
        "ops_by_id": {b["id"]: [{"op": "crop", "x": 0, "y": 0, "width": 0.25, "height": 0.5, "unit": "fraction"}]},
        "output": {"format": "png"}})
    assert res.status_code == 200, res.text
    with zipfile.ZipFile(BytesIO(res.content)) as zf:
        sizes = [Image.open(BytesIO(zf.read(n))).size for n in zf.namelist()]
    assert sizes == [(160, 120), (80, 120)]  # b's list replaces the shared ops entirely


def test_ops_by_id_in_combined_pdf(client):
    a, b = upload_ok(client, ("a.png", encode(photo(320, 240), "PNG")), ("b.png", encode(photo(320, 240), "PNG")))
    res = client.post("/api/process", json={
        "ids": [a["id"], b["id"]],
        "ops_by_id": {a["id"]: [{"op": "crop", "x": 0, "y": 0, "width": 100, "height": 50}]},
        "output": {"format": "pdf", "combine_pdf": True}})
    assert res.status_code == 200, res.text
    boxes = re.findall(rb"/MediaBox\s*\[\s*0 0 ([\d.]+) ([\d.]+)\s*\]", res.content)
    assert [(float(w), float(h)) for w, h in boxes] == [(24.0, 12.0), (76.8, 57.6)]  # px / 300 dpi * 72


def test_ops_by_id_in_preview_and_single_process(client):
    (f,) = upload_ok(client, ("a.png", encode(photo(400, 200), "PNG")))
    body = {"ids": [f["id"]], "ops": [{"op": "rotate", "angle": 90}],
            "ops_by_id": {f["id"]: [{"op": "resize", "mode": "fit", "width": 100}]}, "output": {"format": "png"}}
    data = client.post("/api/preview", json=body).json()
    assert (data["width"], data["height"]) == (100, 50)
    res = client.post("/api/process", json=body)
    assert (res.headers["x-heyp-width"], res.headers["x-heyp-height"]) == ("100", "50")


def test_ops_by_id_unknown_key_is_422(client):
    (f,) = upload_ok(client, ("a.png", encode(photo(), "PNG")))
    res = client.post("/api/process", json={"ids": [f["id"]], "ops_by_id": {"0" * 32: []}})
    assert res.status_code == 422 and "ops_by_id" in res.json()["detail"]


# ------------------------------------------------------------------ v1.1: preview encode / passport (C2)
def test_preview_encode_info(client):
    (f,) = upload_ok(client, ("a.png", encode(photo(600, 600, seed=4), "PNG")))
    plain = client.post("/api/preview", json={"ids": [f["id"]], "output": {"format": "jpeg"}}).json()
    assert plain["encode"] is None and plain["passport"] is None
    data = client.post("/api/preview", json={"ids": [f["id"]], "output": {"format": "jpeg", "max_kb": 25}}).json()
    enc = data["encode"]
    assert set(enc) == {"quality", "scale", "met"} and enc["met"] is True
    assert isinstance(enc["quality"], int) and 0 < enc["scale"] <= 1 and data["size_bytes"] <= 25 * 1024


def test_preview_passport_guides(client, monkeypatch):
    from app.imaging import passport

    def fake_make(img, preset, **kw):
        info = {"dpi": preset.dpi, "face_found": True, "head_ratio": 0.6, "eye_from_bottom": 0.62, "roll_deg": 0.0,
                "face_box_out": {"crown_y": 50.0, "chin_y": 410.0, "eye_y": 228.0}, "warnings": []}
        return Image.new("RGB", preset.output_size(), "white"), info

    monkeypatch.setattr(passport, "analyze", lambda img, *a, **kw: {})
    monkeypatch.setattr(passport, "make", fake_make)
    (f,) = upload_ok(client, ("me.jpg", encode(photo(400, 500))))
    single = client.post("/api/preview", json={"ids": [f["id"]], "ops": [{"op": "passport", "preset": "us"}]}).json()
    guides = single["passport"]
    assert (guides["preset"], guides["width"], guides["height"]) == ("us", single["width"], single["height"])
    assert guides["head_ratio"] == 0.6 and guides["lines"] == {"crown": 50.0, "chin": 410.0, "eye": 228.0}
    assert guides["bands"]["head_min"] == 0.5 and isinstance(guides["background_edit_allowed"], bool)
    sheet = client.post("/api/preview", json={"ids": [f["id"]], "ops": [
        {"op": "passport", "preset": "us"}, {"op": "sheet", "paper": "4x6"}]}).json()
    assert sheet["passport"] is None


def test_preview_passport_source_frame(client, monkeypatch):
    """Context view: ``passport.source`` places the frame on the full-res image entering the passport op."""
    from app.imaging import passport

    monkeypatch.setattr(passport, "analyze", lambda img, *a, **kw: passport.Analysis([], None, img.size))
    (f,) = upload_ok(client, ("me.jpg", encode(photo(400, 500))))
    crop = {"op": "crop", "x": 40, "y": 30, "width": 300, "height": 360}
    pp = {"op": "passport", "preset": "us", "replace_background": False, "zoom": 1.5, "offset_x": -0.1}

    def preview(*ops):
        res = client.post("/api/preview", json={"ids": [f["id"]], "ops": list(ops), "output": {"format": "jpeg"}})
        assert res.status_code == 200, res.text
        return res.json()

    data = preview(crop, pp)
    assert set(data) == PREVIEW_KEYS
    src = data["passport"]["source"]
    assert set(src) == {"width", "height", "center", "size", "angle"}
    # 300×360 after the crop; centred 300 px square at zoom 1.5 → 200 px, subject moved left by 0.1 · 200 px.
    assert src == {"width": 300, "height": 360, "center": [170.0, 180.0], "size": [200.0, 200.0], "angle": 0.0}
    assert src["size"][0] / src["size"][1] == pytest.approx(data["width"] / data["height"])
    # Without a preceding op the source is the upload itself; adjust/background keep the passport object.
    assert preview(pp)["passport"]["source"]["width"] == 400
    assert preview(crop, pp, {"op": "adjust", "contrast": 1.1})["passport"]["source"] == src
    # A later geometry op (sheet, resize …) drops the whole passport object, source included.
    assert preview(crop, pp, {"op": "sheet", "paper": "4x6"})["passport"] is None
    assert preview(crop, pp, {"op": "resize", "mode": "scale", "scale": 50})["passport"] is None


def test_delete_drops_pipeline_caches(client, monkeypatch):
    from app.imaging import pipeline

    monkeypatch.setattr(matting, "available", lambda: False)  # "auto" → the (patched) person model
    monkeypatch.setattr(segment, "available", lambda: True)
    monkeypatch.setattr(segment, "person_mask", lambda img, **kw: np.ones((img.height, img.width), np.float32))
    (f,) = upload_ok(client, ("a.png", encode(photo(), "PNG")))
    assert process(client, [f["id"]], [{"op": "background", "mode": "remove"}], format="png").status_code == 200
    assert any(k.startswith(f["id"]) for k in pipeline._mask_cache.keys())
    assert client.delete(f"/api/files/{f['id']}").status_code == 204
    assert not any(k.startswith(f["id"]) for k in pipeline._mask_cache.keys())


def test_nl_print_sheet_keeps_400_dpi(client):
    """VIS-4: a sheet op without dpi (what the UI sends) prints at the preset's 400 dpi."""
    (f,) = upload_ok(client, ("me.jpg", encode(photo(400, 500))))
    ops = [{"op": "passport", "preset": "nl", "replace_background": False},
           {"op": "sheet", "paper": "4x6", "copies": 0, "cut_lines": True}]
    data = client.post("/api/preview", json={"ids": [f["id"]], "ops": ops}).json()
    assert data["dpi"] == [400, 400] and sorted((data["width"], data["height"])) == [1600, 2400]
    explicit = client.post("/api/preview", json={"ids": [f["id"]], "ops": [ops[0], {**ops[1], "dpi": 300}]}).json()
    assert explicit["dpi"] == [300, 300]


# ------------------------------------------------------------------ robustness review (API-3 … API-17)
from app import api as api_module  # noqa: E402
from app.imaging import pipeline as pipeline_module  # noqa: E402


def test_duplicate_ids_are_processed_once(client, monkeypatch):
    """API-3: [id, id] is one file, not two identical encodes in a ZIP."""
    (f,) = upload_ok(client, ("a.png", encode(photo(), "PNG")))
    runs: list[str] = []
    real_run = pipeline_module.run
    monkeypatch.setattr(pipeline_module, "run", lambda loaded, ops, **kw: runs.append(kw["cache_key"])
                        or real_run(loaded, ops, **kw))
    res = process(client, [f["id"], f["id"], f["id"]], format="png")
    assert res.status_code == 200 and res.headers["content-type"] == "image/png" and runs == [f["id"]]


def test_request_work_budget_is_checked_before_any_work(client, monkeypatch):
    files = upload_ok(client, ("a.png", encode(photo(), "PNG")), ("b.png", encode(photo(), "PNG")))
    monkeypatch.setattr(api_module, "REQUEST_WORK_BUDGET", 0.0001)
    monkeypatch.setattr(pipeline_module, "run", lambda *a, **kw: pytest.fail("ran despite the budget"))
    for path in ("/api/process", "/api/preview"):
        ids = [f["id"] for f in files] if path == "/api/process" else [files[0]["id"]]
        res = client.post(path, json={"ids": ids, "output": {"format": "heic", "max_kb": 10}})
        assert res.status_code == 413 and "too much work" in res.json()["detail"]


def test_plan_errors_name_the_file(client):
    files = upload_ok(client, ("a.png", encode(photo(), "PNG")), ("b.png", encode(photo(), "PNG")))
    res = client.post("/api/process", json={"ids": [f["id"] for f in files],
                                            "ops_by_id": {files[1]["id"]: [{"op": "sheet", "paper": "a0"}]}})
    assert res.status_code == 422 and res.json()["detail"].startswith("b.png: unknown paper")


def test_zip_stops_when_the_client_disconnects(client, monkeypatch):
    files = upload_ok(client, *[(f"{i}.png", encode(photo(seed=i), "PNG")) for i in range(3)])
    runs: list[str] = []
    real_run = pipeline_module.run
    monkeypatch.setattr(pipeline_module, "run", lambda loaded, ops, **kw: runs.append(kw["cache_key"])
                        or real_run(loaded, ops, **kw))
    monkeypatch.setattr(api_module, "_client_gone", lambda request: True)
    for fmt, combine in (("png", False), ("pdf", True)):
        runs.clear()
        res = process(client, [f["id"] for f in files], format=fmt, combine_pdf=combine)
        assert res.status_code == 499 and len(runs) == 1  # only the first item was processed


def test_client_gone_outside_a_worker_thread_is_false():
    assert api_module._client_gone(object()) is False  # type: ignore[arg-type]


def test_batch_survives_ttl_expiry_mid_way(client, monkeypatch):
    """API-12: ids of a running batch are pinned, so the cleanup loop can't expire them half-way."""
    files = upload_ok(client, ("a.png", encode(photo(seed=1), "PNG")), ("b.png", encode(photo(seed=2), "PNG")))
    store = client.app.state.store
    real_run = pipeline_module.run

    def run_while_expiring(loaded, ops, **kw):
        assert store.cleanup_expired(now=time.time() + 10 * 3600) == []
        return real_run(loaded, ops, **kw)

    monkeypatch.setattr(pipeline_module, "run", run_while_expiring)
    res = process(client, [f["id"] for f in files], format="png")
    assert res.status_code == 200
    with zipfile.ZipFile(BytesIO(res.content)) as zf:
        assert zf.namelist() == ["a.png", "b.png"]
    assert sorted(store.ids()) == sorted(f["id"] for f in files)


def test_id_count_uses_the_apps_settings(tmp_path):
    """API-14: create_app(Settings(max_files=2)) limits ids exactly like uploads."""
    with TestClient(create_app(Settings(tmp_dir=str(tmp_path / "s"), max_files=2))) as c:
        ids = [upload_ok(c, (f"{i}.png", encode(photo(seed=i), "PNG")))[0]["id"] for i in range(3)]
        res = process(c, ids, format="png")
        assert res.status_code == 422 and "too many ids: 3 (max 2)" in res.json()["detail"]
        assert process(c, ids[:2], format="png").status_code == 200


def test_preview_cache_only_keeps_bucket_sizes(client):
    """API-8: arbitrary ?max values are rendered but never written to the upload's directory."""
    (f,) = upload_ok(client, ("a.jpg", encode(photo(400, 300))))
    folder = client.app.state.store.root / f["id"]
    for side in (100, 101, 377, 240, 800):  # 800 = this app's preview_max
        res = client.get(f"/api/files/{f['id']}/preview", params={"max": side})
        assert res.status_code == 200
        with Image.open(BytesIO(res.content)) as im:
            assert max(im.size) <= side
    assert sorted(p.name for p in folder.glob("preview-*")) == ["preview-240.jpg", "preview-800.jpg"]


def test_unexpected_errors_keep_security_headers(tmp_path):
    """API-13: the generic 500 handler runs outside the middleware, so it sets the headers itself."""
    app = create_app(Settings(tmp_dir=str(tmp_path / "s")))

    @app.get("/api/boom")
    def boom() -> None:
        raise RuntimeError("boom")

    app.router.routes.insert(0, app.router.routes.pop())  # before the static mount
    with TestClient(app, raise_server_exceptions=False) as c:
        res = c.get("/api/boom")
    assert res.status_code == 500 and res.json() == {"detail": "Internal server error"}
    assert res.headers["content-security-policy"] == CSP
    assert res.headers["x-content-type-options"] == "nosniff" and res.headers["referrer-policy"] == "no-referrer"


def test_json_body_over_the_cap_is_413(client):
    """API-7: oversized JSON is refused before it is read and parsed."""
    (f,) = upload_ok(client, ("a.png", encode(photo(), "PNG")))
    body = json.dumps({"ids": [f["id"]], "output": {"filename": "x" * (3 * 1024 * 1024)}})
    res = client.post("/api/process", content=body, headers={"content-type": "application/json"})
    assert res.status_code == 413 and "too large" in res.json()["detail"]
    assert res.headers["content-security-policy"] == CSP


def test_upload_body_over_the_cap_is_413(tmp_path, monkeypatch):
    """API-6: a multipart body above the request cap is refused before it is spooled or stored."""
    import app.config as config_module

    monkeypatch.setattr(config_module, "UPLOAD_REQUEST_MIN_MB", 1)
    with TestClient(create_app(Settings(tmp_dir=str(tmp_path / "s"), max_upload_mb=1))) as c:
        assert c.app.state.settings.max_request_bytes == 2 * 1024 * 1024
        res = upload(c, ("a.bin", os.urandom(1024 * 1024)), ("b.bin", os.urandom(1024 * 1024 + 10)))
        assert res.status_code == 413 and "send fewer files" in res.json()["detail"]
        assert c.app.state.store.ids() == []
        assert upload(c, ("ok.png", encode(photo(), "PNG"))).status_code == 200


def test_chunked_body_over_the_cap_is_413(tmp_path):
    """API-6/7: without a Content-Length the body is counted as it streams in."""
    import anyio

    app = create_app(Settings(tmp_dir=str(tmp_path / "s")))
    chunks = [{"type": "http.request", "body": b"[" + b" " * (1024 * 1024), "more_body": True}] * 3
    sent: list[dict] = []

    async def receive() -> dict:
        return chunks.pop() if chunks else {"type": "http.request", "body": b"]", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": "/api/process", "raw_path": b"/api/process", "query_string": b"",
             "root_path": "", "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")],
             "client": ("127.0.0.1", 1), "server": ("testserver", 80), "state": {}}
    anyio.run(app, scope, receive, send)
    assert sent[0]["status"] == 413 and b"too large" in sent[1]["body"]


def test_zip_on_a_full_disk_is_507_not_500(client, monkeypatch):
    """API-10: closing the spool re-raises ENOSPC; that must not replace the mapped error."""
    import errno as errno_module

    files = upload_ok(client, ("a.png", encode(photo(seed=1), "PNG")), ("b.png", encode(photo(seed=2), "PNG")))

    real_spool = api_module.tempfile.SpooledTemporaryFile

    class FullSpool(real_spool):  # type: ignore[misc, valid-type]
        def write(self, data):
            if self.tell() + len(data) > 1000:
                raise OSError(errno_module.ENOSPC, "No space left on device")
            return super().write(data)

        def close(self):
            super().close()
            raise OSError(errno_module.ENOSPC, "No space left on device")

    monkeypatch.setattr(api_module.tempfile, "SpooledTemporaryFile", FullSpool)
    res = process(client, [f["id"] for f in files], format="png")
    assert res.status_code == 507 and res.json()["detail"] == "a.png: Server storage full"


def test_zip_size_is_capped(client, monkeypatch):
    files = upload_ok(client, ("a.png", encode(photo(seed=1), "PNG")), ("b.png", encode(photo(seed=2), "PNG")))
    assert api_module.ZIP_MIN_BYTES <= api_module._zip_cap(client.app.state.store.root) <= api_module.ZIP_MAX_BYTES
    monkeypatch.setattr(api_module, "_zip_cap", lambda root: 300_000)  # one 320×240 BMP is ~230 KB
    res = process(client, [f["id"] for f in files], format="bmp")
    assert res.status_code == 413 and "The ZIP would be larger" in res.json()["detail"]


def test_large_preview_output_is_not_decoded_at_full_size(client, monkeypatch):
    """API-17: above DISPLAY_DECODE_MAX_PIXELS a JPEG is draft-decoded, other formats use the pre-encode image."""
    (f,) = upload_ok(client, ("a.png", encode(photo(1200, 900), "PNG")))
    monkeypatch.setattr(api_module, "DISPLAY_DECODE_MAX_PIXELS", 10_000)
    loads: list[object] = []
    real_load = imgio.load
    monkeypatch.setattr(imgio, "load", lambda src: loads.append(src) or real_load(src))
    for fmt in ("jpeg", "png"):
        data = client.post("/api/preview", params={"max": 300}, json={"ids": [f["id"]], "output": {"format": fmt}}).json()
        assert (data["width"], data["height"]) == (1200, 900)
        with Image.open(BytesIO(base64.b64decode(data["image"].split(",", 1)[1]))) as im:
            assert im.size == (300, 225)
    assert not any(isinstance(src, bytes) for src in loads)


def test_job_slots_bound_the_queue_and_memory():
    """API-5: waiting is bounded (new requests get Busy), memory-planned jobs run within the budget."""
    slots = api_module.JobSlots(max_jobs=2, memory_bytes=100, max_waiting=1)
    first = slots.acquire(60, timeout=1)
    assert first == 60
    got: list[int | None] = []
    waiter = threading.Thread(target=lambda: got.append(slots.acquire(60, timeout=5)))
    waiter.start()
    deadline = time.time() + 5
    while slots.stats()["waiting"] != 1 and time.time() < deadline:
        time.sleep(0.01)
    assert slots.stats() == {"running": 1, "waiting": 1, "planned_bytes": 60}  # 60 + 60 > 100: must wait
    with pytest.raises(api_module.Busy):
        slots.acquire(1, timeout=1)  # a new request finds the queue full
    assert slots.acquire(1, timeout=0.05, bounded=False) is None  # admitted work queues behind (FIFO) → timeout
    slots.release(first)
    waiter.join()
    assert got == [60]
    slots.release(60)
    assert slots.acquire(10 ** 9, timeout=1) == 100  # bigger than the whole budget: clamped, runs alone
    assert slots.acquire(1, timeout=0.05) is None
    slots.release(100)


def test_full_job_queue_is_503_with_retry_after(client):
    (f,) = upload_ok(client, ("a.png", encode(photo(), "PNG")))
    slots = api_module.JobSlots(max_jobs=1, memory_bytes=1 << 30, max_waiting=1)
    client.app.state.jobs = slots
    held = slots.acquire(0, timeout=1)
    waiter = threading.Thread(target=lambda: slots.release(slots.acquire(0, timeout=5, bounded=False)))
    waiter.start()
    deadline = time.time() + 5
    while slots.stats()["waiting"] != 1 and time.time() < deadline:
        time.sleep(0.01)
    res = process(client, [f["id"]], format="png")
    assert res.status_code == 503 and res.headers["retry-after"] == "5"
    assert client.get(f"/api/files/{f['id']}/preview", params={"max": 77}).status_code == 503
    assert client.delete(f"/api/files/{f['id']}").status_code == 204  # never queues behind jobs
    slots.release(held)
    waiter.join()


def test_batch_exports_report_per_file_warnings(client):
    """ZIP and combined PDF carry "<name>: <warning>" in X-Heyp-Warnings (single files: unprefixed)."""
    a, b = upload_ok(client, ("a.jpg", encode(photo(400, 500, seed=1))), ("b.jpg", encode(photo(400, 500, seed=2))))
    passport = [{"op": "passport", "preset": "us", "replace_background": False}]
    for output in ({"format": "jpeg"}, {"format": "pdf", "combine_pdf": True}):
        res = client.post("/api/process", json={"ids": [a["id"], b["id"]], "ops": passport, "output": output})
        assert res.status_code == 200, res.text
        warnings = json.loads(unquote(res.headers["x-heyp-warnings"]))
        assert any(w.startswith("a.jpg: No face") for w in warnings), warnings  # synthetic photos have no face
        assert any(w.startswith("b.jpg: No face") for w in warnings), warnings


def test_warnings_header_is_capped_to_whole_entries():
    long = [f"photo-{i}.jpg: {'x' * 80}" for i in range(200)]
    value = api_module.warnings_header(long)
    kept = json.loads(unquote(value))
    assert len(value) <= api_module.WARNINGS_HEADER_MAX + 100
    assert kept[:-1] == long[:len(kept) - 1] and kept[-1].startswith("…and ") and kept[-1].endswith("more warnings")
    assert json.loads(unquote(api_module.warnings_header(["a", "b"]))) == ["a", "b"]


def test_faces_without_a_detector_is_503(client, monkeypatch):
    from app.imaging import face

    (f,) = upload_ok(client, ("a.png", encode(photo(200, 150), "PNG")))
    monkeypatch.setattr(face, "available", lambda: False)
    res = client.get(f"/api/files/{f['id']}/faces")
    assert res.status_code == 503 and "unavailable" in res.json()["detail"]
