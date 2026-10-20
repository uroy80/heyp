"""app.imaging.ops: colour/ratio parsing, geometry, trim, every resize mode, pad, adjust, saliency."""
from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from app.imaging import ops
from tests.conftest import QUADRANT_COLORS, close_color, make_image, quadrant_colors


# ------------------------------------------------------------------ parsing
@pytest.mark.parametrize("text,expected", [
    ("#fff", (255, 255, 255, 255)), ("#FF0000", (255, 0, 0, 255)), ("#00ff0080", (0, 255, 0, 128)),
    ("#0000", (0, 0, 0, 0)), ("white", (255, 255, 255, 255)), ("RebeccaPurple", (102, 51, 153, 255)),
    ("transparent", (0, 0, 0, 0)), ("  #abc  ", (170, 187, 204, 255)), ("rgb(1, 2, 3)", (1, 2, 3, 255)),
    ((10, 20, 30), (10, 20, 30, 255)), ([1, 2, 3, 4], (1, 2, 3, 4)),
])
def test_parse_color(text, expected):
    assert ops.parse_color(text) == expected


@pytest.mark.parametrize("bad", ["", "#12", "#gggggg", "notacolour", None, 12, (300, 0, 0), (1, 2)])
def test_parse_color_invalid(bad):
    with pytest.raises(ValueError):
        ops.parse_color(bad)


@pytest.mark.parametrize("value,expected", [
    ("16:9", 16 / 9), ("4/5", 0.8), ("3x2", 1.5), ("1.5", 1.5), (1.5, 1.5), (2, 2.0), (" 35:45 ", 35 / 45),
])
def test_parse_ratio(value, expected):
    assert ops.parse_ratio(value) == pytest.approx(expected)


@pytest.mark.parametrize("bad", ["", "abc", "1:0", "0:1", "-4:3", 0, -1.5, float("nan"), float("inf"), "1000:1",
                                 None, True])
def test_parse_ratio_invalid(bad):
    with pytest.raises(ValueError):
        ops.parse_ratio(bad)


@pytest.mark.parametrize("value,unit,dpi,expected", [
    (100, "px", 300, 100), (2, "in", 300, 600), (2.54, "cm", 300, 300), (35, "mm", 300, 413), (10.4, "px", 1, 10),
])
def test_to_px(value, unit, dpi, expected):
    assert ops.to_px(value, unit, dpi) == expected


def test_to_px_invalid():
    with pytest.raises(ValueError):
        ops.to_px(1, "ft", 300)
    with pytest.raises(ValueError):
        ops.to_px(1, "in", 0)


# ------------------------------------------------------------------ rotate / flip
def test_rotate_is_clockwise_and_lossless():
    img = make_image((40, 20), kind="quadrants")
    right = ops.rotate(img, 90)
    assert right.size == (20, 40)
    corners = quadrant_colors(right, inset=2)
    assert corners["tl"] == QUADRANT_COLORS["bl"]
    assert corners["tr"] == QUADRANT_COLORS["tl"]
    assert np.array_equal(np.asarray(right), np.asarray(img.transpose(Image.Transpose.ROTATE_270)))
    assert np.array_equal(np.asarray(ops.rotate(img, -90)), np.asarray(ops.rotate(img, 270)))
    assert np.array_equal(np.asarray(ops.rotate(img, 180)), np.asarray(img.transpose(Image.Transpose.ROTATE_180)))
    assert np.array_equal(np.asarray(ops.rotate(img, 450)), np.asarray(right))
    same = ops.rotate(img, 360)
    assert same is not img and np.array_equal(np.asarray(same), np.asarray(img))


def test_rotate_arbitrary_angle_expands_and_fills():
    img = Image.new("RGB", (100, 50), (0, 0, 255))
    out = ops.rotate(img, 30, fill="#ff0000")
    w = math.ceil(100 * math.cos(math.radians(30)) + 50 * math.sin(math.radians(30)))
    assert abs(out.width - w) <= 1
    assert close_color(out.getpixel((0, 0)), (255, 0, 0), 2)
    assert close_color(out.getpixel((out.width // 2, out.height // 2)), (0, 0, 255), 2)
    assert ops.rotate(img, 30, expand=False).size == (100, 50)


def test_rotate_transparent_fill_and_gray():
    out = ops.rotate(Image.new("RGB", (40, 40), "white"), 10, fill="transparent")
    assert out.mode == "RGBA" and out.getpixel((0, 0))[3] == 0
    gray = ops.rotate(Image.new("L", (40, 40), 128), 10, fill="#ffffff")
    assert gray.mode == "L" and gray.getpixel((0, 0)) == 255
    coloured = ops.rotate(Image.new("L", (40, 40), 128), 10, fill="#ff0000")
    assert coloured.mode == "RGB" and coloured.getpixel((0, 0)) == (255, 0, 0)
    rgba = ops.rotate(make_image((40, 40), "RGBA"), 15)
    assert rgba.mode == "RGBA"


def test_rotate_invalid():
    with pytest.raises(ValueError):
        ops.rotate(make_image(), float("nan"))
    with pytest.raises(ValueError):
        ops.rotate(make_image(), "abc")


def test_flip():
    img = make_image((40, 20), kind="quadrants")
    h = quadrant_colors(ops.flip(img, "horizontal"), 2)
    v = quadrant_colors(ops.flip(img, "vertical"), 2)
    assert h["tl"] == QUADRANT_COLORS["tr"] and v["tl"] == QUADRANT_COLORS["bl"]
    with pytest.raises(ValueError):
        ops.flip(img, "diagonal")


# ------------------------------------------------------------------ crop
def test_crop_px_and_fraction():
    img = make_image((200, 100))
    assert ops.crop(img, 10, 20, 50, 30).size == (50, 30)
    out = ops.crop(img, 0.25, 0.5, 0.5, 0.5, unit="fraction")
    assert out.size == (100, 50)
    assert out.getpixel((0, 0)) == img.getpixel((50, 50))


def test_crop_clamps_to_bounds_and_keeps_one_pixel():
    img = make_image((200, 100))
    assert ops.crop(img, -50, -50, 100, 100).size == (50, 50)
    assert ops.crop(img, 150, 50, 500, 500).size == (50, 50)
    assert ops.crop(img, 0.5, 0.5, 2.0, 2.0, unit="fraction").size == (100, 50)
    assert ops.crop(img, 10, 10, 0.2, 0.2).size == (1, 1)
    assert ops.crop(img, 0.1, 0.1, 0.0001, 0.0001, unit="fraction").size == (1, 1)


@pytest.mark.parametrize("args", [(300, 0, 10, 10), (-20, 0, 10, 10), (0, 0, 0, 10), (0, 0, 10, -5),
                                  (float("nan"), 0, 10, 10)])
def test_crop_invalid(args):
    with pytest.raises(ValueError):
        ops.crop(make_image((200, 100)), *args)
    with pytest.raises(ValueError):
        ops.crop(make_image((200, 100)), 0, 0, 10, 10, unit="cm")


# ------------------------------------------------------------------ crop_aspect / smart_box
@pytest.mark.parametrize("ratio,expected", [("16:9", (4000, 2250)), ("1:1", (3000, 3000)), ("4:5", (2400, 3000)),
                                            (4 / 3, (4000, 3000)), ("9:16", (1688, 3000))])
def test_crop_aspect_sizes(ratio, expected):
    img = Image.new("RGB", (4000, 3000))
    assert ops.crop_aspect(img, ratio).size == expected


def _busy_patch_image(size=(400, 200), patch_x=320) -> Image.Image:
    img = Image.new("RGB", size, (128, 128, 128))
    noise = make_image((60, 60), kind="noise", seed=3)
    img.paste(noise, (patch_x, 70))
    return img


def test_crop_aspect_center_and_smart():
    img = _busy_patch_image()
    center = ops.crop_aspect(img, "1:1", anchor="center")
    assert center.size == (200, 200)
    assert np.asarray(center).std() < 1  # the busy patch is outside the centred crop
    smart = ops.crop_aspect(img, "1:1", anchor="smart")
    assert np.asarray(smart).std() > 20
    x, _ = ops.smart_box(img, 200, 200)
    assert x <= 320 and x + 200 >= 380


def test_crop_aspect_face_anchor():
    img = Image.new("RGB", (400, 200), (128, 128, 128))
    face = {"box": [10, 60, 40, 40], "score": 0.9}
    small = SimpleNamespace(box=(300, 10, 10, 10))
    out = ops.crop_aspect(img, "1:1", anchor="face", faces=[small, face])
    assert out.size == (200, 200)
    x, _ = ops.smart_box(img, 200, 200)  # uniform image → smart is centred
    assert x == 100
    # Face at the far left: the crop is clamped to x = 0, so it contains the face.
    marked = img.copy()
    marked.paste((255, 0, 0), (10, 60, 50, 100))
    out = ops.crop_aspect(marked, "1:1", anchor="face", faces=[face])
    assert out.getpixel((30, 80)) == (255, 0, 0)


def test_crop_aspect_face_falls_back_to_smart():
    img = _busy_patch_image()
    out = ops.crop_aspect(img, "1:1", anchor="face", faces=[])
    assert np.asarray(out).std() > 20
    out = ops.crop_aspect(img, "1:1", anchor="face", faces=[{"box": "bad"}, {"nobox": 1}])
    assert np.asarray(out).std() > 20


def test_crop_aspect_invalid():
    with pytest.raises(ValueError):
        ops.crop_aspect(make_image(), "1:1", anchor="top")
    with pytest.raises(ValueError):
        ops.crop_aspect(make_image(), "nope")


def test_smart_box_bounds_and_alpha():
    img = make_image((1000, 50), kind="noise")
    for w, h in ((10, 10), (999, 50), (1000, 50), (5000, 5000)):
        x, y = ops.smart_box(img, w, h)
        assert 0 <= x <= max(0, 1000 - min(w, 1000)) and 0 <= y <= max(0, 50 - min(h, 50))
    rgba = Image.new("RGBA", (300, 100), (0, 0, 0, 0))
    rgba.paste(make_image((50, 50), "RGB", kind="noise").convert("RGBA"), (20, 25))
    x, _ = ops.smart_box(rgba, 100, 100)
    assert x <= 20 and x + 100 >= 70


# ------------------------------------------------------------------ trim
def _bordered(mode="RGB", border=(255, 255, 255), inner=(200, 30, 30), size=(200, 120), box=(30, 20, 150, 90)):
    img = Image.new(mode, size, border)
    img.paste(inner, box)
    return img


def test_trim_uniform_border():
    out = ops.trim(_bordered())
    assert out.size == (120, 70)
    assert ops.trim(_bordered(), padding=5).size == (130, 80)
    assert ops.trim(_bordered(), padding=500).size == (200, 120)


def test_trim_tolerance_handles_noisy_border():
    img = _bordered()
    arr = np.asarray(img).astype(int)
    rng = np.random.default_rng(0)
    arr = np.clip(arr - rng.integers(0, 8, arr.shape), 0, 255).astype(np.uint8)
    noisy = Image.fromarray(arr)
    assert ops.trim(noisy, tolerance=12).size == (120, 70)
    assert ops.trim(noisy, tolerance=0).size == (200, 120)


def test_trim_transparent_border_and_gray():
    img = Image.new("RGBA", (100, 80), (0, 0, 0, 0))
    img.paste((0, 0, 0, 255), (10, 10, 60, 50))  # black content on transparent black: alpha decides
    assert ops.trim(img).size == (50, 40)
    gray = _bordered("L", border=0, inner=200)
    assert ops.trim(gray).size == (120, 70)


def test_trim_corner_majority_and_uniform():
    img = _bordered()
    img.putpixel((0, 0), (0, 0, 0))  # one odd corner; the other three agree on white
    assert ops.trim(img).size == (150, 90)  # white wins; the odd black pixel itself is content
    flat = Image.new("RGB", (50, 40), "white")
    out = ops.trim(flat)
    assert out.size == (50, 40) and out is not flat


def test_trim_invalid():
    with pytest.raises(ValueError):
        ops.trim(make_image(), tolerance=300)
    with pytest.raises(ValueError):
        ops.trim(make_image(), padding=-1)


# ------------------------------------------------------------------ resize
SRC = (4000, 3000)


@pytest.mark.parametrize("mode,kwargs,expected", [
    ("fit", {"width": 1200, "height": 1200}, (1200, 900)),
    ("fit", {"width": 1200}, (1200, 900)),
    ("fit", {"height": 600}, (800, 600)),
    ("fit", {"width": 2000, "height": 600}, (800, 600)),
    ("fill", {"width": 1000, "height": 1000}, (1000, 1000)),
    ("fill", {"width": 1920, "height": 1080}, (1920, 1080)),
    ("pad", {"width": 1000, "height": 1000}, (1000, 1000)),
    ("exact", {"width": 333, "height": 777}, (333, 777)),
    ("scale", {"scale": 50}, (2000, 1500)),
    ("scale", {"scale": 12.5}, (500, 375)),
    ("long_edge", {"size": 1000}, (1000, 750)),
    ("short_edge", {"size": 1000}, (1333, 1000)),
    ("megapixels", {"megapixels": 3}, (2000, 1500)),
    ("fit", {"width": 4, "height": 6, "unit": "in", "dpi": 100}, (400, 300)),
    ("exact", {"width": 35, "height": 45, "unit": "mm", "dpi": 300}, (413, 531)),
    ("long_edge", {"size": 2.54, "unit": "cm", "dpi": 300}, (300, 225)),
    ("fit", {"width": 8000, "upscale": True}, (8000, 6000)),
])
def test_resize_modes_exact_sizes(mode, kwargs, expected):
    img = Image.new("RGB", SRC, (10, 20, 30))
    assert ops.resize(img, mode, **kwargs).size == expected


def test_resize_portrait_edges():
    img = Image.new("RGB", (3000, 4000))
    assert ops.resize(img, "long_edge", size=1000).size == (750, 1000)
    assert ops.resize(img, "short_edge", size=1000).size == (1000, 1333)


@pytest.mark.parametrize("mode,kwargs,expected", [
    ("fit", {"width": 8000, "height": 8000}, (400, 300)),
    ("scale", {"scale": 200}, (400, 300)),
    ("long_edge", {"size": 1000}, (400, 300)),
    ("short_edge", {"size": 1000}, (400, 300)),
    ("megapixels", {"megapixels": 1}, (400, 300)),
    ("exact", {"width": 800, "height": 200}, (400, 200)),
    ("pad", {"width": 1000, "height": 1000}, (1000, 1000)),
    ("fill", {"width": 1000, "height": 1000}, (300, 300)),
    ("fill", {"width": 600, "height": 100}, (400, 67)),
    ("fit", {"width": 200}, (200, 150)),
])
def test_resize_never_upscales_when_disabled(mode, kwargs, expected):
    img = Image.new("RGB", (400, 300))
    assert ops.resize(img, mode, upscale=False, **kwargs).size == expected


def test_resize_pad_background_and_content():
    img = Image.new("RGB", (400, 200), (255, 0, 0))
    out = ops.resize(img, "pad", width=300, height=300, background="#00ff00")
    assert out.getpixel((0, 0)) == (0, 255, 0)
    assert out.getpixel((150, 150)) == (255, 0, 0)
    small = ops.resize(img, "pad", width=1000, height=1000, upscale=False)
    assert small.getpixel((500, 500)) == (255, 0, 0) and small.getpixel((10, 10)) == (255, 255, 255)
    clear = ops.resize(img, "pad", width=300, height=300, background="transparent")
    assert clear.mode == "RGBA" and clear.getpixel((0, 0))[3] == 0


def test_resize_fill_crops_centre():
    img = Image.new("RGB", (300, 100), (255, 0, 0))
    img.paste((0, 0, 255), (100, 0, 200, 100))
    out = ops.resize(img, "fill", width=50, height=50)
    assert close_color(out.getpixel((25, 25)), (0, 0, 255), 4)


def test_resize_never_below_one_pixel():
    img = Image.new("RGB", (4000, 10))
    assert ops.resize(img, "long_edge", size=100).size == (100, 1)
    assert ops.resize(img, "scale", scale=0.001).size == (1, 1)


@pytest.mark.parametrize("resample", list(ops.RESAMPLE))
def test_resize_resample_filters(resample):
    out = ops.resize(make_image((200, 100), "RGBA"), "scale", scale=37, resample=resample)
    assert out.size == (74, 37) and out.mode == "RGBA"


@pytest.mark.parametrize("mode,kwargs", [
    ("fill", {"width": 100}), ("pad", {"height": 100}), ("exact", {}), ("fit", {}), ("scale", {}),
    ("long_edge", {}), ("megapixels", {}), ("zoom", {"scale": 2}), ("fit", {"width": -5}),
    ("fit", {"width": 0}), ("scale", {"scale": float("nan")}), ("fit", {"width": 10, "unit": "ft"}),
    ("fit", {"width": 10, "resample": "magic"}), ("fit", {"width": 2, "unit": "in", "dpi": 0}),
    ("fit", {"width": "wide"}),
])
def test_resize_invalid_combinations(mode, kwargs):
    with pytest.raises(ValueError):
        ops.resize(make_image((100, 100)), mode, **kwargs)


def test_resize_respects_pixel_limit(pixel_limit):
    pixel_limit(1_000_000)
    with pytest.raises(ValueError, match="limit"):
        ops.resize(make_image((100, 100)), "exact", width=2000, height=2000)
    with pytest.raises(ValueError, match="limit"):
        ops.pad(make_image((100, 100)), top=2000, left=2000)
    assert ops.resize(make_image((100, 100)), "exact", width=900, height=900).size == (900, 900)


# ------------------------------------------------------------------ pad
def test_pad():
    img = Image.new("RGB", (10, 20), (255, 0, 0))
    out = ops.pad(img, top=1, right=2, bottom=3, left=4, color="#0000ff")
    assert out.size == (16, 24)
    assert out.getpixel((0, 0)) == (0, 0, 255) and out.getpixel((4, 1)) == (255, 0, 0)
    clear = ops.pad(img, 5, 5, 5, 5, color="transparent")
    assert clear.mode == "RGBA" and clear.getpixel((0, 0)) == (0, 0, 0, 0)
    assert ops.pad(img).size == (10, 20)
    with pytest.raises(ValueError):
        ops.pad(img, top=-1)
    with pytest.raises(ValueError):
        ops.pad(img, top=1.5)


# ------------------------------------------------------------------ adjust
def test_adjust_identity_and_factors():
    img = make_image((40, 30), kind="photo")
    same = ops.adjust(img)
    assert same is not img and np.array_equal(np.asarray(same), np.asarray(img))
    assert np.asarray(ops.adjust(img, brightness=0)).max() == 0
    gray = np.asarray(ops.adjust(img, saturation=0)).astype(int)
    assert np.abs(gray[..., 0] - gray[..., 1]).max() <= 1
    flat = np.asarray(ops.adjust(img, contrast=0)).astype(int)
    assert flat.std() < 1
    assert ops.adjust(img, sharpness=2).size == img.size
    assert ops.adjust(img, grayscale=True).mode == "L"


def test_adjust_keeps_alpha():
    img = make_image((40, 30), "RGBA")
    out = ops.adjust(img, brightness=0.5, contrast=1.2, grayscale=True)
    assert out.mode == "LA"
    assert np.array_equal(np.asarray(out)[..., 1], np.asarray(img)[..., 3])
    assert ops.adjust(make_image((10, 10), "LA"), brightness=1.5).mode == "LA"


def test_adjust_auto_contrast_stretches():
    arr = np.full((20, 20, 3), 100, np.uint8)
    arr[:10] = 140
    out = np.asarray(ops.adjust(Image.fromarray(arr), auto_contrast=True))
    assert out.min() < 60 and out.max() > 200


@pytest.mark.parametrize("kwargs", [{"brightness": -1}, {"contrast": float("inf")}, {"saturation": 1000},
                                    {"sharpness": "x"}])
def test_adjust_invalid(kwargs):
    with pytest.raises(ValueError):
        ops.adjust(make_image(), **kwargs)


def test_resize_pad_without_upscale_keeps_the_canvas_but_not_the_content_size():
    """upscale=false never enlarges the picture; pad still delivers the requested canvas (ImageMagick-style
    ``-resize WxH> -extent WxH``), with the unscaled photo centred."""
    img = Image.new("RGB", (1000, 500), (255, 0, 0))
    out = ops.resize(img, "pad", width=4000, height=4000, upscale=False, background="#000000")
    assert out.size == (4000, 4000)
    assert out.getbbox() == (1500, 1750, 2500, 2250)
    shrunk = ops.resize(img, "pad", width=400, height=400, upscale=False, background="#000000")
    assert shrunk.size == (400, 400) and shrunk.getbbox() == (0, 100, 400, 300)
