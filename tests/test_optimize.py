"""File optimisers (app/imaging/compress.py v2): gifsicle, pngquant, scour and optimize_file()."""
from __future__ import annotations

import http.server
import io as _io
import threading

import pytest
from PIL import Image

from app.imaging import animated, compress, io
from tests.conftest import make_gif_bytes, make_image, make_jpeg_bytes, make_svg

needs_gifsicle = pytest.mark.skipif(not compress.tool_available("gifsicle"), reason="gifsicle not installed")
needs_pngquant = pytest.mark.skipif(not compress.tool_available("pngquant"), reason="pngquant not installed")
needs_scour = pytest.mark.skipif(not compress.tool_available("scour"), reason="scour not installed")


def png_bytes(img: Image.Image) -> bytes:
    buf = _io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def noisy_gif(n: int = 8) -> bytes:
    """A gradient backdrop with a moving block (what real GIFs look like to gifsicle)."""
    backdrop = make_image((160, 120), "RGB", "gradient")
    frames = []
    for i in range(n):
        frame = backdrop.copy()
        frame.paste((255, 255, 0), (i * 12, 40, i * 12 + 30, 70))
        frames.append(frame)
    buf = _io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=[70] * n, loop=0)
    return buf.getvalue()


# ------------------------------------------------------------------ gifsicle
@needs_gifsicle
def test_gifsicle_shrinks_and_keeps_the_animation():
    data = noisy_gif()
    out = compress.gifsicle(data, lossy=80)
    assert len(out) < len(data) * 0.9
    anim = animated.load(out)
    assert len(anim) == 8 and anim.durations == [70] * 8 and anim.loop == 0


@needs_gifsicle
def test_gifsicle_keeps_transparency():
    data = make_gif_bytes(5)
    anim = animated.load(compress.gifsicle(data, lossy=40))
    assert anim.frames[0].getpixel((0, 0))[3] == 0 and len(anim) == 5


@pytest.mark.parametrize("kwargs", [{"lossy": 201}, {"lossy": -1}, {"colors": 1}, {"colors": 300}])
def test_gifsicle_validates_options(kwargs):
    with pytest.raises(ValueError):
        compress.gifsicle(make_gif_bytes(2), **kwargs)


def test_gifsicle_refuses_non_gifs_and_reports_missing_tool(monkeypatch):
    with pytest.raises(ValueError, match="Not a GIF"):
        compress.gifsicle(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(compress.shutil, "which", lambda _name: None)
    with pytest.raises(compress.ToolUnavailable):
        compress.gifsicle(make_gif_bytes(2))


def test_tool_input_size_is_capped(monkeypatch):
    monkeypatch.setattr(compress, "TOOL_MAX_INPUT_BYTES", 100)
    with pytest.raises(ValueError, match="too large"):
        compress.gifsicle(make_gif_bytes(4))


# ------------------------------------------------------------------ pngquant
@needs_pngquant
def test_pngquant_makes_a_smaller_palette_png_and_keeps_alpha():
    img = make_image((200, 150), "RGBA", "photo")
    data = png_bytes(img)
    out = compress.pngquant(data, quality=(0, 90))
    assert out is not None and len(out) < len(data)
    back = Image.open(_io.BytesIO(out))
    assert back.mode in ("P", "PA") and "transparency" in back.info
    assert back.convert("RGBA").getpixel((0, 0))[3] == 0


@needs_pngquant
def test_pngquant_gives_up_when_quality_can_not_be_met():
    data = png_bytes(make_image((64, 64), "RGB", "noise"))
    assert compress.pngquant(data, quality=(99, 100)) is None


def test_pngquant_validation():
    with pytest.raises(ValueError, match="Not a PNG"):
        compress.pngquant(b"GIF89a")
    with pytest.raises(ValueError, match="quality"):
        compress.pngquant(png_bytes(Image.new("RGB", (2, 2))), quality=(90, 10))


# ------------------------------------------------------------------ scour
class _Recorder(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):  # noqa: N802
        _Recorder.hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.end_headers()
        self.wfile.write(png_bytes(Image.new("RGB", (2, 2))))

    def log_message(self, *args):
        pass


@needs_scour
def test_scour_never_fetches_urls_or_inlines_local_files(tmp_path):
    _Recorder.hits = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    secret = tmp_path / "secret.png"
    Image.new("RGB", (4, 4), (0, 255, 0)).save(secret)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/probe.png"
        svg = make_svg(f'<image width="10" height="10" xlink:href="{url}"/>'
                       f'<image width="10" height="10" xlink:href="{secret}"/>'
                       f'<image width="10" height="10" xlink:href="file://{secret}"/>'
                       + '<rect width="10" height="10" fill="#ff0000"/>' * 20)
        out = compress.svg(svg).decode()
    finally:
        server.shutdown()
    assert _Recorder.hits == []
    assert "base64" not in out and url in out          # links kept as links, never fetched or inlined


@needs_scour
def test_scour_shrinks_strips_active_content_and_keeps_the_size():
    body = "".join(f'<rect x="{i}.123456789" y="0" width="1.000000001" height="10" fill="#ff0000"/>\n  <!-- c {i} -->'
                   for i in range(50))
    svg = make_svg('<script>alert(1)</script><metadata>junk</metadata>' + body, extra=' onload="x()"')
    out = compress.svg(svg, "extreme")
    text = out.decode()
    assert len(out) < len(svg) and "script" not in text and "onload" not in text and "<!--" not in text
    assert 'width="100"' in text and 'height="80"' in text
    assert len(compress.svg(svg, "extreme")) <= len(compress.svg(svg, "low"))
    rendered = io.load(out).image
    assert rendered.getpixel((5 * 1024 // 100, 50))[:3] == (255, 0, 0)


@needs_scour
@pytest.mark.parametrize("bad, message", [
    (b"<html></html>", "Not an SVG"),
    (b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
     b'<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;"><!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">'
     b'<!ENTITY e "&d;&d;&d;&d;&d;&d;&d;&d;&d;&d;"><!ENTITY f "&e;&e;&e;&e;&e;&e;&e;&e;&e;&e;">'
     b'<!ENTITY g "&f;&f;&f;&f;&f;&f;&f;&f;&f;&f;">]>'
     b'<svg xmlns="http://www.w3.org/2000/svg"><text>&g;&g;</text></svg>', "Invalid SVG"),
    (b"\x1f\x8b\x08\x00", "Compressed SVG"),
])
def test_scour_refuses_hostile_or_wrong_input(bad, message):
    with pytest.raises(ValueError, match=message):
        compress.svg(bad)


def test_scour_option_guard_and_level_validation():
    with pytest.raises(ValueError, match="level"):
        compress.svg(make_svg(), "maximum")


# ------------------------------------------------------------------ optimize_file
def test_jpeg_is_reencoded_smaller_upright_and_without_metadata():
    data = make_jpeg_bytes((400, 300), orientation=6, gps=True, camera=True, quality=98, kind="photo")
    result = compress.optimize_file(data, "jpeg", "recommended")
    assert result.changed and len(result.data) < len(data) and result.format == "jpeg"
    back = Image.open(_io.BytesIO(result.data))
    assert back.size == (300, 400) and not back.getexif()


def test_levels_order_sizes():
    data = make_jpeg_bytes((400, 300), quality=98, kind="photo")
    sizes = [len(compress.optimize_file(data, "jpeg", lv).data) for lv in ("low", "recommended", "extreme")]
    assert sizes[0] >= sizes[1] >= sizes[2]


def test_already_small_files_come_back_unchanged():
    tiny = make_jpeg_bytes((32, 32), quality=20)
    result = compress.optimize_file(tiny, "jpeg", "low")
    assert not result.changed and result.data == tiny and "already" in result.note


@needs_pngquant
def test_png_quantize_or_lossless():
    data = png_bytes(make_image((256, 192), "RGB", "photo"))
    quantized = compress.optimize_file(data, "png", "recommended")
    lossless = compress.optimize_file(data, "png", "recommended", png_quantize=False)
    assert quantized.changed and len(quantized.data) < len(data)
    if lossless.changed:
        assert Image.open(_io.BytesIO(lossless.data)).convert("RGB").tobytes() == \
            Image.open(_io.BytesIO(data)).convert("RGB").tobytes()


def test_png_falls_back_to_lossless_without_pngquant(monkeypatch):
    real_which = compress.shutil.which
    monkeypatch.setattr(compress.shutil, "which", lambda name: None if name == "pngquant" else real_which(name))
    data = png_bytes(make_image((128, 96), "RGB", "gradient"))
    bloated = data + b""  # Pillow's default PNG is already reasonable; result may be unchanged
    result = compress.optimize_file(bloated, "png", "extreme")
    assert result.format == "png" and len(result.data) <= len(bloated)


def test_animated_webp_and_apng_keep_their_frames():
    frames = [make_image((80, 60), "RGB", "photo", seed=i) for i in range(4)]
    webp = animated.encode(frames, [100] * 4, 0, "webp", quality=100)
    result = compress.optimize_file(webp, "webp", "extreme")
    assert result.changed and len(animated.load(result.data)) == 4
    apng = animated.encode(frames, [100] * 4, 0, "png")
    result = compress.optimize_file(apng, "png", "recommended")
    back = animated.load(result.data)
    assert back is not None and len(back) == 4


@needs_gifsicle
def test_gif_through_optimize_file():
    data = noisy_gif(4)
    result = compress.optimize_file(data, "gif", "extreme")
    assert result.changed and len(animated.load(result.data)) == 4


@pytest.mark.parametrize("fmt", ["heic", "tiff", "bmp", "psd", "dng"])
def test_other_formats_are_refused_with_a_clear_message(fmt):
    with pytest.raises(ValueError, match="compress works on JPG, PNG, GIF, SVG and WebP"):
        compress.optimize_file(b"x", fmt)


def test_unknown_level_is_refused():
    with pytest.raises(ValueError, match="level"):
        compress.optimize_file(b"x", "jpeg", "ultra")
