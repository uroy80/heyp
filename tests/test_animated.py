"""Animated GIF / WebP / APNG (app/imaging/animated.py): decode, encode, round trips, frame bombs."""
from __future__ import annotations

import io as _io
import time

import pytest
from PIL import Image

from app.imaging import animated, io, pipeline
from tests.conftest import make_frame_bomb_gif, make_gif_bytes


def frames(n=4, size=(40, 30), alpha=False):
    out = []
    for i in range(n):
        f = Image.new("RGBA" if alpha else "RGB", size, (0, 0, 0, 0) if alpha else (20, 20, 20))
        f.paste((255, 40 * i, 0), (i * 5, 5, i * 5 + 10, 15))
        out.append(f)
    return out


def test_probe_and_load_keep_frames_durations_loop_and_transparency():
    data = make_gif_bytes(6, (60, 40), loop=3)
    assert animated.probe(data) == ("gif", 6, 60, 40)
    assert animated.is_animated(data)
    anim = animated.load(data)
    assert len(anim) == 6 and anim.size == (60, 40) and anim.loop == 3
    assert anim.durations == [40, 50, 60, 70, 80, 90]
    assert anim.frames[0].mode == "RGBA" and anim.frames[0].getpixel((0, 0))[3] == 0


def test_stills_are_not_animations():
    buf = _io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, "GIF")
    assert animated.load(buf.getvalue()) is None and not animated.is_animated(buf.getvalue())
    buf = _io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, "JPEG")
    assert animated.probe(buf.getvalue()) is None


def test_gif_without_loop_extension_plays_once_and_round_trips():
    data = make_gif_bytes(3, loop=None)
    anim = animated.load(data)
    assert anim.loop is None
    again = animated.load(animated.encode(anim.frames, anim.durations, anim.loop, "gif"))
    assert again.loop is None and len(again) == 3


@pytest.mark.parametrize("fmt", ["gif", "webp", "png"])
@pytest.mark.parametrize("alpha", [False, True])
def test_encode_round_trips(fmt, alpha):
    src = frames(4, alpha=alpha)
    data = animated.encode(src, [100, 200, 300, 400], 0, fmt)
    back = animated.load(data)
    assert back.format == fmt and len(back) == 4 and back.durations == [100, 200, 300, 400] and back.loop == 0
    assert back.size == (40, 30)
    px = back.frames[2].convert("RGBA").getpixel((15, 10))
    assert px[0] > 200 and abs(px[1] - 80) < 40
    if alpha:
        assert back.frames[0].convert("RGBA").getpixel((39, 29))[3] == 0


def test_gif_durations_are_centiseconds_with_a_floor():
    back = animated.load(animated.encode(frames(2), [5, 1234], 0, "gif"))
    assert back.durations == [20, 1230]


@pytest.mark.parametrize("kwargs, message", [
    ({"fmt": "jpeg"}, "GIF, WebP or PNG"),
    ({"durations": [100]}, "one entry per frame"),
    ({"quality": 0}, "quality"),
    ({"loop": 70000}, "loop"),
])
def test_encode_validation(kwargs, message):
    args = {"frames": frames(2), "durations": [100, 100], "loop": 0, "fmt": "gif"}
    args.update(kwargs)
    with pytest.raises(ValueError, match=message):
        animated.encode(args["frames"], args["durations"], args["loop"], args["fmt"],
                        **{k: v for k, v in kwargs.items() if k == "quality"})


def test_encode_refuses_mixed_sizes_and_empty():
    with pytest.raises(ValueError, match="same size"):
        animated.encode([Image.new("RGB", (5, 5)), Image.new("RGB", (6, 5))], [1, 1], 0, "gif")
    with pytest.raises(ValueError, match="No frames"):
        animated.encode([], [], 0, "gif")


def test_apply_maps_every_frame_and_insists_on_one_size():
    anim = animated.load(make_gif_bytes(3))
    flipped = animated.apply(anim, lambda f: f.transpose(Image.Transpose.FLIP_LEFT_RIGHT))
    assert len(flipped) == 3 and flipped.durations == anim.durations
    sizes = iter([(10, 10), (11, 10), (10, 10)])
    with pytest.raises(ValueError, match="different sizes"):
        animated.apply(anim, lambda f: f.resize(next(sizes)))


# ------------------------------------------------------------------ frame bombs (critic blocking #4)
def test_frame_bomb_is_refused_from_the_header_before_decoding():
    bomb = make_frame_bomb_gif(600, (7000, 7000))      # 112 KB-class file, 29 400 MP decoded
    assert len(bomb) < 20_000
    start = time.monotonic()
    with pytest.raises(io.ImageError) as err:
        animated.load(bomb)
    assert err.value.status == 413 and "too large" in str(err.value)
    assert time.monotonic() - start < 5


def test_too_many_frames_is_refused():
    with pytest.raises(io.ImageError) as err:
        animated.load(make_frame_bomb_gif(animated.MAX_FRAMES + 1, (10, 10)))
    assert err.value.status == 413 and "frames" in str(err.value)


def test_anim_pixel_budget_comes_from_the_environment(monkeypatch):
    data = make_gif_bytes(5, (60, 40))
    monkeypatch.setenv("HEYP_MAX_ANIM_PIXELS", str(5 * 60 * 40 - 1))
    with pytest.raises(io.ImageError):
        animated.load(data)
    monkeypatch.setenv("HEYP_MAX_ANIM_PIXELS", "junk")
    assert animated.max_anim_pixels() == animated.DEFAULT_MAX_ANIM_PIXELS
    assert len(animated.load(data)) == 5


def test_pipeline_guard_uses_the_same_limits():
    with pytest.raises(pipeline.PipelineError) as err:
        pipeline.load_animation(make_frame_bomb_gif(600, (7000, 7000)))
    assert err.value.status == 413


def test_psd_layers_are_never_iterated_as_frames():
    from tests.conftest import make_psd_bytes

    data = make_psd_bytes(layers=3)
    assert animated.load(data) is None and animated.probe(data) is None
    assert io.load(data).frames == 1


def test_truncated_animation_is_a_clean_415():
    data = make_gif_bytes(6)
    with pytest.raises(io.ImageError) as err:
        animated.load(data[: len(data) // 2])
    assert err.value.status in (413, 415)


def _raw_gif_durations(data: bytes) -> list[int]:
    with Image.open(_io.BytesIO(data)) as im:
        out = []
        for i in range(im.n_frames):
            im.seek(i)
            out.append(int(im.info.get("duration") or 0))
        return out


def test_gif_one_centisecond_delays_keep_the_speed_browsers_show():
    """img-08: browsers play 0/1 cs GIF delays at 100 ms; an edit must not speed the animation up 5–10×."""
    src = make_gif_bytes(4, durations=[10, 10, 10, 10])
    assert _raw_gif_durations(src) == [10, 10, 10, 10]
    anim = animated.load(src)
    assert anim.durations == [100, 100, 100, 100]
    assert _raw_gif_durations(animated.encode(anim.frames, anim.durations, anim.loop, "gif")) == [100] * 4
    webp = animated.load(animated.encode(anim.frames, anim.durations, anim.loop, "webp"))
    assert webp.durations == [100] * 4
    # 20 ms is honoured by browsers and kept; a WebP source's own 10 ms delays are not GIF-clamped
    assert animated.load(make_gif_bytes(3, durations=[20, 20, 20])).durations == [20, 20, 20]
    fast_webp = animated.encode(frames(3), [10, 10, 10], 0, "webp")
    assert animated.load(fast_webp).durations == [10, 10, 10]


def test_an_animation_exported_to_a_still_format_says_so(tmp_path):
    """img-04: a GIF resized to JPEG/PNG keeps one frame — the response must warn, and GIF/WebP/original keep it moving."""
    import json
    from urllib.parse import unquote

    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    settings = Settings(tmp_dir=str(tmp_path / "heyp"), max_upload_mb=5, max_files=10, max_jobs=2, cache_items=4,
                        preview_max=800, ttl_minutes=60)
    with TestClient(create_app(settings)) as client:
        res = client.post("/api/files", files=[("files", ("a.gif", make_gif_bytes(6, (60, 40)), "image/gif"))])
        file_id = res.json()["files"][0]["id"]
        ops = [{"op": "resize", "mode": "fit", "width": 30}]
        for fmt in ("jpeg", "png"):
            res = client.post("/api/process", json={"ids": [file_id], "ops": ops, "output": {"format": fmt}})
            assert res.status_code == 200, res.text
            warnings = json.loads(unquote(res.headers["x-heyp-warnings"]))
            assert any("first frame" in w for w in warnings), (fmt, warnings)
            preview = client.post("/api/preview", json={"ids": [file_id], "ops": ops, "output": {"format": fmt}})
            assert any("first frame" in w for w in preview.json()["warnings"])
        for fmt in ("gif", "webp", "original"):
            res = client.post("/api/process", json={"ids": [file_id], "ops": ops, "output": {"format": fmt}})
            assert res.status_code == 200, res.text
            assert not any("first frame" in w for w in json.loads(unquote(res.headers["x-heyp-warnings"])))
            assert animated.probe(res.content)[1] == 6
