"""heyp job tools and asset routes (app/tools/img_tools.py): img.make_gif, img.compress, /api/assets."""
from __future__ import annotations

import io as _io
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.core import registry
from app.core.errors import ToolError
from app.core.jobs import make_job_manager
from app.imaging import animated, compress, io
from app.store import FileStore
from app.tools import img_tools
from tests.conftest import make_gif_bytes, make_image, make_jpeg_bytes, make_svg


@pytest.fixture
def runner(tmp_path):
    settings = Settings.from_env({"HEYP_TMP_DIR": str(tmp_path / "heyp")})
    store = FileStore(tmp_path / "heyp" / "files", ttl=3600)
    store.init()
    manager = make_job_manager(settings, store)
    manager.start()
    yield manager, store
    manager.stop()


def upload(store: FileStore, data: bytes, name: str) -> str:
    stored = store.save(name, _io.BytesIO(data), max_bytes=50 * 1024 * 1024)
    head = data[:65536]
    fmt = io.sniff(head) or io.canonical_format(Image.open(_io.BytesIO(data)).format)
    store.set_info(stored.id, kind="image", format=fmt, mime=io.mime_for(fmt))
    return stored.id


def png(img: Image.Image) -> bytes:
    buf = _io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def wait(job, timeout: float = 60.0) -> dict:
    end = time.monotonic() + timeout
    while not job.finished_flag and time.monotonic() < end:
        time.sleep(0.02)
    assert job.finished_flag, job.to_json()
    return job.to_json()


def result_bytes(store: FileStore, info: dict) -> bytes:
    return store.get(info["id"]).path.read_bytes()


# ------------------------------------------------------------------ registry
def test_tools_are_registered_with_sane_schemas():
    gif = registry.get("img.make_gif")
    assert gif.category == "Create" and gif.lane == "heavy" and gif.max_files >= 100
    schema = registry.options_schema(gif)
    assert schema["properties"]["delay_ms"]["default"] == 500 and schema["properties"]["loop"]["type"] == "boolean"
    assert set(schema["properties"]["format"]["enum"]) == {"gif", "webp"}
    comp = registry.get("img.compress")
    assert registry.options_schema(comp)["properties"]["level"]["default"] == "recommended"
    assert registry.tool_json(comp)["available"] is True


# ------------------------------------------------------------------ img.make_gif
def test_make_gif_in_order_with_timing_and_loop(runner):
    manager, store = runner
    colours = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    ids = [upload(store, png(Image.new("RGB", (80, 60), c)), f"f{i}.png") for i, c in enumerate(colours)]
    job = manager.submit("img.make_gif", ids, {"delay_ms": 250, "loop": True, "order": [2, 0, 1, 0]})
    body = wait(job)
    assert body["status"] == "done", body
    out = body["result"]["files"][0]
    assert out["name"] == "f2_make_gif.gif"
    anim = animated.load(result_bytes(store, out))
    assert len(anim) == 4 and anim.durations == [250] * 4 and anim.loop == 0 and anim.size == (80, 60)
    firsts = [f.convert("RGB").getpixel((40, 30)) for f in anim.frames]
    assert [max(range(3), key=lambda k: px[k]) for px in firsts] == [2, 0, 1, 0]
    assert body["result"]["data"]["frames"] == 4


def test_make_gif_webp_play_once_resized_and_letterboxed(runner):
    manager, store = runner
    ids = [upload(store, png(Image.new("RGB", (400, 200), (255, 0, 0))), "wide.png"),
           upload(store, png(Image.new("RGB", (100, 200), (0, 0, 255))), "tall.png")]
    job = manager.submit("img.make_gif", ids, {"format": "webp", "loop": False, "size": 200, "background": "#000000"})
    body = wait(job)
    assert body["status"] == "done", body
    anim = animated.load(result_bytes(store, body["result"]["files"][0]))
    assert anim.format == "webp" and anim.size == (200, 100) and len(anim) == 2
    second = anim.frames[1].convert("RGB")
    assert second.getpixel((5, 50))[2] < 40 and second.getpixel((100, 50))[2] > 200   # letterbox, then the photo


def test_make_gif_cover_crops(runner):
    manager, store = runner
    ids = [upload(store, png(Image.new("RGB", (100, 100), (0, 0, 0))), "a.png"),
           upload(store, png(make_image((300, 100), "RGB", "quadrants")), "b.png")]
    body = wait(manager.submit("img.make_gif", ids, {"fit": "cover"}))
    anim = animated.load(result_bytes(store, body["result"]["files"][0]))
    assert anim.size == (100, 100)
    assert anim.frames[1].convert("RGB").getpixel((2, 50)) != (0, 0, 0)  # no bars


def test_make_gif_shrinks_to_the_animation_budget(runner, monkeypatch):
    manager, store = runner
    monkeypatch.setattr(img_tools, "MAKE_GIF_MAX_PIXELS", 3 * 100 * 100)
    ids = [upload(store, png(Image.new("RGB", (400, 400), (9, 9, 9))), f"{i}.png") for i in range(3)]
    body = wait(manager.submit("img.make_gif", ids, {}))
    assert body["status"] == "done" and any("limit" in w for w in body["warnings"])
    assert body["result"]["data"]["width"] <= 100


def test_make_gif_rejects_bad_options(runner):
    manager, store = runner
    fid = upload(store, png(Image.new("RGB", (10, 10))), "x.png")
    for options in ({"delay_ms": 5}, {"format": "mp4"}, {"order": [-1]}, {"size": 99999}, {"surprise": 1}):
        with pytest.raises(ToolError) as err:
            manager.submit("img.make_gif", [fid], options)
        assert err.value.status == 422
    body = wait(manager.submit("img.make_gif", [fid], {"order": [0, 5]}))
    assert body["status"] == "failed" and body["error"]["status"] == 422 and "file #5" in body["error"]["detail"]


def test_make_gif_keeps_transparency(runner):
    manager, store = runner
    clear = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    clear.paste((255, 0, 0, 255), (10, 10, 30, 30))
    moved = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    moved.paste((0, 0, 255, 255), (2, 2, 12, 12))
    ids = [upload(store, png(clear), "a.png"), upload(store, png(moved), "b.png")]
    body = wait(manager.submit("img.make_gif", ids, {"background": "transparent"}))
    anim = animated.load(result_bytes(store, body["result"]["files"][0]))
    assert anim.frames[0].getpixel((0, 0))[3] == 0 and anim.frames[0].getpixel((20, 20))[:3] == (255, 0, 0)
    assert anim.frames[1].getpixel((20, 20))[3] == 0 and anim.frames[1].getpixel((5, 5))[:3] == (0, 0, 255)


# ------------------------------------------------------------------ img.compress
def test_compress_mixed_batch_keeps_formats_and_reports_savings(runner):
    manager, store = runner
    frames = []
    backdrop = make_image((160, 120), "RGB", "gradient")
    for i in range(5):
        f = backdrop.copy()
        f.paste((255, 255, 0), (i * 20, 40, i * 20 + 30, 70))
        frames.append(f)
    gif_buf = _io.BytesIO()
    frames[0].save(gif_buf, "GIF", save_all=True, append_images=frames[1:], duration=[80] * 5, loop=0)
    svg = make_svg("".join(f'<rect x="{i}.000001" y="0" width="1.0000001" height="10" fill="#f00"/><!-- {i} -->'
                           for i in range(40)))
    ids = [upload(store, make_jpeg_bytes((300, 200), quality=98, kind="photo"), "photo.jpg"),
           upload(store, png(make_image((200, 150), "RGBA", "photo")), "shot.png"),
           upload(store, gif_buf.getvalue(), "anim.gif"), upload(store, svg, "icon.svg")]
    body = wait(manager.submit("img.compress", ids, {"level": "recommended"}))
    assert body["status"] == "done", body
    names = [f["name"] for f in body["result"]["files"]]
    assert names == ["photo_compress.jpg", "shot_compress.png", "anim_compress.gif", "icon_compress.svg"]
    rows = body["result"]["data"]["files"]
    assert all(r["after"] < r["before"] and r["changed"] for r in rows), rows
    assert 0 < body["result"]["data"]["saved"] < 1
    gif = animated.load(result_bytes(store, body["result"]["files"][2]))
    assert len(gif) == 5


def test_compress_skips_unsupported_files_with_a_warning(runner):
    manager, store = runner
    tiff = _io.BytesIO()
    Image.new("RGB", (20, 20)).save(tiff, "TIFF")
    ids = [upload(store, tiff.getvalue(), "scan.tif"), upload(store, make_jpeg_bytes((300, 200), quality=98), "a.jpg")]
    body = wait(manager.submit("img.compress", ids, {}))
    assert body["status"] == "done" and len(body["result"]["files"]) == 1
    assert any("scan.tif" in w for w in body["warnings"])
    only_tiff = wait(manager.submit("img.compress", [ids[0]], {}))
    assert only_tiff["status"] == "failed" and only_tiff["error"]["status"] == 422


def test_compress_returns_already_small_files_unchanged(runner):
    manager, store = runner
    tiny = make_jpeg_bytes((16, 16), quality=10)
    body = wait(manager.submit("img.compress", [upload(store, tiny, "tiny.jpg")], {"level": "low"}))
    row = body["result"]["data"]["files"][0]
    assert row["changed"] is False and row["after"] == row["before"]
    assert result_bytes(store, body["result"]["files"][0]) == tiny


def test_compress_level_is_validated(runner):
    manager, store = runner
    fid = upload(store, make_jpeg_bytes(), "a.jpg")
    with pytest.raises(ToolError):
        manager.submit("img.compress", [fid], {"level": "insane"})


# ------------------------------------------------------------------ assets router
@pytest.fixture(scope="module")
def client():
    app = FastAPI()
    app.include_router(img_tools.router, prefix="/api")
    return TestClient(app)


def test_catalog(client):
    res = client.get("/api/assets/catalog")
    assert res.status_code == 200 and "max-age" in res.headers["cache-control"]
    body = res.json()
    assert len(body["filters"]) >= 18 and len(body["frames"]) >= 9 and len(body["stickers"]) >= 120
    assert "mosaic" in body["positions"] and body["canvases"][0]["id"] == "square"
    assert set(body["optimizers"]) == {"gifsicle", "pngquant", "scour"}
    sticker = body["stickers"][0]
    assert client.get(sticker["url"]).status_code == 200


def test_sticker_is_served_sandboxed(client):
    res = client.get("/api/assets/stickers/openmoji/grinning-face.svg")
    assert res.status_code == 200 and res.headers["content-type"].startswith("image/svg+xml")
    assert "sandbox" in res.headers["content-security-policy"] and res.headers["x-content-type-options"] == "nosniff"
    assert res.text.lstrip().startswith("<svg")


@pytest.mark.parametrize("path", ["/api/assets/stickers/openmoji/nope.svg", "/api/assets/stickers/..%2F..%2Fx/y.svg",
                                  "/api/assets/stickers/openmoji/..%2Fmanifest.svg",
                                  "/api/assets/stickers/OPENMOJI/grinning-face.svg"])
def test_unknown_or_traversing_stickers_are_404(client, path):
    assert client.get(path).status_code == 404


def test_fonts_are_served_by_id_only(client):
    res = client.get("/api/assets/fonts/anton.ttf")
    assert res.status_code == 200 and res.headers["content-type"] == "font/ttf" and res.content[:4] == b"\x00\x01\x00\x00"
    assert client.get("/api/assets/fonts/libre-franklin.ttf").status_code == 200
    assert client.get("/api/assets/fonts/..%2F..%2Fapp%2Fmain.py.ttf").status_code == 404
    assert client.get("/api/assets/fonts/arial.ttf").status_code == 404


def test_optimizer_flags_follow_the_tools(monkeypatch):
    monkeypatch.setattr(compress.shutil, "which", lambda _name: None)
    assert img_tools.catalog()["optimizers"]["gifsicle"] is False
    assert make_gif_bytes(2)  # factory sanity
