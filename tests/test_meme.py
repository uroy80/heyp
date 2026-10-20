"""Meme captions (app/imaging/meme.py): placement, auto size, outline, outside bands, limits."""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from app.imaging import meme

BLUE = (30, 60, 160)


def rows_with(img: Image.Image, rgb: tuple[int, int, int], tol: int = 40) -> np.ndarray:
    arr = np.asarray(img.convert("RGB"), np.int16)
    hit = (np.abs(arr - np.array(rgb)).sum(axis=2) < tol)
    return np.where(hit.any(axis=1))[0]


def test_top_and_bottom_captions_sit_at_the_edges():
    img = Image.new("RGB", (600, 400), BLUE)
    out = meme.apply(img, {"top": "top text", "bottom": "bottom text"})
    white = rows_with(out, (255, 255, 255))
    assert white.min() >= 400 * 0.02 and white.min() < 400 * 0.12
    assert white.max() <= 400 * 0.98 and white.max() > 400 * 0.85
    assert not ((white > 400 * 0.3) & (white < 400 * 0.7)).any()   # nothing in the middle
    assert out.size == img.size and out.mode == "RGB"


def test_outline_black_by_default_and_none_disables_it():
    img = Image.new("RGB", (600, 300), BLUE)
    outlined = meme.apply(img, {"top": "HELLO"})
    plain = meme.apply(img, {"top": "HELLO", "outline": "none"})
    assert len(rows_with(outlined, (0, 0, 0), 30)) > 0
    assert len(rows_with(plain, (0, 0, 0), 30)) == 0


def test_uppercase_option():
    img = Image.new("RGB", (600, 300), BLUE)
    upper = meme.apply(img, {"top": "abc", "outline": "none"})
    lower = meme.apply(img, {"top": "abc", "outline": "none", "uppercase": False})
    assert not np.array_equal(np.asarray(upper), np.asarray(lower))


def test_auto_size_fits_the_width_and_caps_at_14_percent():
    img = Image.new("RGB", (800, 400), BLUE)
    short = meme.prepare({"top": "HI", "outline": "none"}, img.size).top
    assert short.height <= 400 * 0.14 * 1.6  # one line at ≤ 14 % of the height (plus line gap)
    long_caption = "this caption is far too long to fit on one line at a readable size, so it wraps"
    layer = meme.prepare({"top": long_caption}, img.size).top
    assert layer.width <= 800 * 0.94 + 2
    assert layer.height > short.height * 0.8  # wrapped onto several readable lines


def test_explicit_size_is_a_share_of_the_height():
    small = meme.prepare({"top": "X", "size": 0.05, "outline": "none"}, (500, 400)).top
    big = meme.prepare({"top": "X", "size": 0.2, "outline": "none"}, (500, 400)).top
    assert big.height > small.height * 3


def test_outside_layout_adds_bands_and_keeps_the_photo():
    img = Image.new("RGB", (500, 300), BLUE)
    out = meme.apply(img, {"top": "above", "bottom": "below", "layout": "outside"})
    assert out.width == 500 and out.height > 300
    band = meme.prepare({"top": "above", "bottom": "below", "layout": "outside"}, img.size)
    assert out.size == band.output_size(img.size)
    top_h = band.top.height + 2 * band.pad
    assert out.getpixel((250, top_h + 150)) == BLUE           # the photo, untouched
    assert out.getpixel((2, 2)) == (255, 255, 255)            # white band
    assert len(rows_with(out.crop((0, 0, 500, top_h)), (0, 0, 0), 60)) > 0   # black caption text


def test_alpha_is_kept_inside():
    img = Image.new("RGBA", (300, 200), (0, 0, 0, 0))
    out = meme.apply(img, {"top": "A"})
    assert out.mode == "RGBA" and out.getpixel((150, 100))[3] == 0


def test_blank_canvases():
    assert meme.canvas("square").size == (1080, 1080)
    assert meme.canvas("landscape", "#00000000").mode == "RGBA"
    with pytest.raises(ValueError):
        meme.canvas("billboard")


@pytest.mark.parametrize("op, message", [
    ({"top": "", "bottom": "  "}, "caption"),
    ({"top": "x", "layout": "sideways"}, "layout"),
    ({"top": "x", "font": "wingdings"}, "font"),
])
def test_invalid_memes(op, message):
    with pytest.raises(ValueError, match=message):
        meme.apply(Image.new("RGB", (100, 100)), op)


def test_huge_caption_on_tiny_image_stays_bounded():
    out = meme.apply(Image.new("RGB", (16, 16), BLUE), {"top": "W" * 300, "size": 0.5})
    assert out.size == (16, 16)


# ------------------------------------------------------------------ img-03: never wider or taller than allowed
LONG = ("when you finally fix the bug at three in the morning and then realise the test was wrong all along "
        "and the original code was fine the whole time")


@pytest.mark.parametrize("size", [(600, 1000), (300, 300), (1080, 1080), (1080, 1920), (100, 2000)])
@pytest.mark.parametrize("caption", [LONG, LONG + " " + LONG[:60], "W" * 40, "SUPERCALIFRAGILISTIC" * 3])
def test_auto_captions_fit_inside_the_width_and_height_share(size, caption):
    W, H = size
    prepared = meme.prepare({"top": caption, "bottom": caption}, size)
    for layer in (prepared.top, prepared.bottom):
        assert layer.width <= W * 0.94 + 2, (size, layer.size)
        assert layer.height <= H * 0.3 + 2, (size, layer.size)
    out = meme.stamp(Image.new("RGB", size, BLUE), prepared)
    assert out.size == size


def test_explicit_lines_are_capped_to_the_height_share():
    for caption in ("ONE\nTWO\nTHREE\nFOUR", "A\nB\nC\nD\nE", "1\n2\n3\n4\n5\n6\n7\n8"):
        prepared = meme.prepare({"top": caption, "bottom": "BOTTOM"}, (800, 600))
        assert prepared.top.height <= 600 * 0.3 + 2, (caption, prepared.top.size)
        assert prepared.top.height + prepared.bottom.height + 2 * prepared.pad < 600   # never overlap


def test_a_caption_set_below_the_floor_warns():
    tiny = meme.prepare({"top": LONG + " " + LONG}, (300, 300))
    assert tiny.warnings and "top caption is long" in tiny.warnings[0]
    assert meme.prepare({"top": "short and sweet"}, (800, 600)).warnings == []
    wrapped = meme.prepare({"top": "this caption is long enough to wrap onto two lines"}, (800, 400))
    assert wrapped.warnings == [] and wrapped.top.width <= 800 * 0.94 + 2


def test_landscape_layout_is_unchanged_for_ordinary_captions():
    # one line at the 14 % cap for a short caption; a long one wraps at a readable size (≥ the floor)
    short = meme.prepare({"top": "HI", "outline": "none"}, (800, 400)).top
    assert 400 * 0.14 * 0.9 <= short.height <= 400 * 0.14 * 1.6
    px, wrap_width, small = meme._caption_size(LONG.upper(), "anton", 800 * 0.94, None, 400, 0.07)
    assert wrap_width is not None and px >= 400 * 0.035 and not small
