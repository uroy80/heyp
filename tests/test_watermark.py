"""Watermark engine (app/imaging/watermark.py): placement, mosaic, logo marks, opacity, limits."""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from app.imaging import watermark

GREY = (90, 90, 90)


def changed_box(before: Image.Image, after: Image.Image) -> tuple[int, int, int, int] | None:
    diff = np.abs(np.asarray(after.convert("RGB"), np.int16) - np.asarray(before.convert("RGB"), np.int16)).sum(axis=2)
    ys, xs = np.where(diff > 30)
    if not len(xs):
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


@pytest.mark.parametrize("position, region", [
    ("top-left", (0, 0, 0.5, 0.5)), ("top-right", (0.5, 0, 1, 0.5)), ("bottom-left", (0, 0.5, 0.5, 1)),
    ("bottom-right", (0.5, 0.5, 1, 1)), ("center", (0.25, 0.25, 0.75, 0.75)), ("top", (0.25, 0, 0.75, 0.5)),
    ("left", (0, 0.25, 0.5, 0.75)), ("right", (0.5, 0.25, 1, 0.75)), ("bottom", (0.25, 0.5, 0.75, 1)),
])
def test_text_mark_lands_in_its_grid_cell(position, region):
    img = Image.new("RGB", (400, 300), GREY)
    out = watermark.apply(img, {"kind": "text", "text": "© heyp", "position": position, "opacity": 1, "size": 0.08})
    box = changed_box(img, out)
    assert box is not None
    x0, y0, x1, y1 = region
    assert box[0] >= x0 * 400 - 1 and box[2] <= x1 * 400 + 1 and box[1] >= y0 * 300 - 1 and box[3] <= y1 * 300 + 1


def test_margin_and_size_are_fractions_of_the_shorter_side():
    for size in ((600, 300), (300, 600)):
        img = Image.new("RGB", size, GREY)
        out = watermark.apply(img, {"kind": "text", "text": "MARK", "position": "bottom-right", "opacity": 1,
                                    "size": 0.1, "margin": 0.05, "font": "anton"})
        box = changed_box(img, out)
        short = min(size)
        assert size[0] - box[2] >= 0.05 * short - 2 and size[1] - box[3] >= 0.05 * short - 2
        assert size[0] - box[2] <= 0.05 * short + 6
        assert 0.05 * short <= box[3] - box[1] <= 0.15 * short  # cap height of a 0.1 × short font


def test_custom_position_centres_the_mark():
    img = Image.new("RGB", (400, 400), GREY)
    out = watermark.apply(img, {"kind": "text", "text": "X", "position": "custom", "x": 0.25, "y": 0.75,
                                "opacity": 1, "size": 0.1})
    x0, y0, x1, y1 = changed_box(img, out)
    assert abs((x0 + x1) / 2 - 100) < 12 and abs((y0 + y1) / 2 - 300) < 16


def test_mosaic_covers_the_whole_picture_with_a_rotated_mark():
    img = Image.new("RGB", (500, 400), GREY)
    out = np.asarray(watermark.apply(img, {"kind": "text", "text": "heyp", "position": "mosaic", "opacity": 1,
                                           "rotation": 30, "size": 0.06}))
    marked = np.abs(out.astype(int) - GREY[0]).sum(axis=2) > 30
    for qy in range(2):
        for qx in range(2):
            assert marked[qy * 200:(qy + 1) * 200, qx * 250:(qx + 1) * 250].mean() > 0.01


def test_opacity_blends_and_alpha_is_kept():
    img = Image.new("RGBA", (300, 200), (0, 0, 0, 255))
    full = watermark.apply(img, {"kind": "text", "text": "■■■", "position": "center", "opacity": 1, "size": 0.2,
                                 "color": "#ffffff", "font": "dejavu-sans"})
    half = watermark.apply(img, {"kind": "text", "text": "■■■", "position": "center", "opacity": 0.5, "size": 0.2,
                                 "color": "#ffffff", "font": "dejavu-sans"})
    assert full.mode == "RGBA"
    assert np.asarray(full)[..., 0].max() == 255
    assert 110 <= np.asarray(half)[..., 0].max() <= 140


def test_image_watermark_scales_to_a_share_of_the_width():
    logo = Image.new("RGBA", (100, 50), (255, 0, 0, 255))
    img = Image.new("RGB", (800, 600), GREY)
    out = watermark.apply(img, {"kind": "image", "image_id": "a" * 32, "image_scale": 0.25, "opacity": 1,
                                "position": "top-left", "margin": 0}, load_image=lambda _id: logo)
    x0, y0, x1, y1 = changed_box(img, out)
    assert (x0, y0) == (0, 0) and abs((x1 - x0) - 200) <= 1 and abs((y1 - y0) - 100) <= 1
    assert out.getpixel((10, 10)) == (255, 0, 0)


@pytest.mark.parametrize("op, message", [
    ({"kind": "text", "text": "  "}, "text"),
    ({"kind": "image"}, "image"),
    ({"kind": "image", "image_id": "x"}, "not available"),
    ({"kind": "video", "text": "x"}, "kind"),
    ({"kind": "text", "text": "x", "position": "nowhere"}, "position"),
    ({"kind": "text", "text": "x", "opacity": 2}, "opacity"),
])
def test_invalid_watermarks_are_refused(op, message):
    with pytest.raises(ValueError, match=message):
        watermark.apply(Image.new("RGB", (100, 100)), op)


def test_tiny_mosaic_is_refused_rather_than_tiling_forever():
    img = Image.new("RGB", (8000, 8000), GREY)
    with pytest.raises(ValueError, match="too many copies"):
        watermark.apply(img, {"kind": "text", "text": ".", "position": "mosaic", "size": 0.002, "spacing": 0})


def test_absurd_logo_aspect_is_refused():
    logo = Image.new("RGBA", (1, 2000), (255, 0, 0, 255))
    with pytest.raises(ValueError, match="far larger"):
        watermark.apply(Image.new("RGB", (1000, 1000)), {"kind": "image", "image_id": "a" * 32, "image_scale": 1},
                        load_image=lambda _id: logo)
