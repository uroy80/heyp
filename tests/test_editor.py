"""Photo editor engine (app/imaging/editor.py): filters, frames, overlay items, stickers, catalogue."""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.imaging import editor
from tests.conftest import make_image

RED = (220, 40, 40)


def arr(img: Image.Image) -> np.ndarray:
    return np.asarray(img).astype(np.int16)


# ------------------------------------------------------------------ filters
def test_there_are_about_eighteen_or_more_filters_and_the_ui_ids_exist():
    ui = {"mono", "noir", "sepia", "vintage", "warm", "cool", "vivid", "fade", "matte", "dramatic", "polaroid",
          "kodachrome", "technicolor", "lomo", "bright", "sharpen", "soft", "invert"}
    assert ui <= set(editor.FILTERS) and len(editor.FILTERS) >= 18


def test_the_ui_catalogue_names_every_server_filter_and_frame_the_same_way():
    """static/js/catalog.js keeps its own list (swatch CSS, frame defaults): one missing there showed an unstyled
    swatch, and picking a frame sent the UI's colour instead of the style's own (a white film strip)."""
    src = (Path(__file__).resolve().parents[1] / "static/js/catalog.js").read_text(encoding="utf-8")
    def labels(const: str) -> dict[str, str]:
        block = src.split(f"export const {const} = [", 1)[1].split("];", 1)[0]
        return dict(re.findall(r"\{ id: '([\w-]+)', label: '([^']+)'", block))

    filters, frames = labels("FILTERS"), labels("FRAMES")
    assert {fid: spec.name for fid, spec in editor.FILTERS.items()}.items() <= filters.items()
    assert {fid: spec["name"] for fid, spec in editor.FRAMES.items()}.items() <= frames.items()


@pytest.mark.parametrize("name", list(editor.FILTERS))
def test_every_filter_changes_the_picture_and_keeps_size_and_alpha(name):
    img = make_image((96, 64), "RGBA", "photo", seed=3)
    out = editor.apply_filter(img, name)
    assert out.size == img.size and out.mode == "RGBA"
    assert np.array_equal(np.asarray(out.getchannel("A")), np.asarray(img.getchannel("A")))
    assert np.abs(arr(out.convert("RGB")) - arr(img.convert("RGB"))).mean() > 1.0


def test_intensity_zero_is_identity_and_half_is_between():
    img = make_image((64, 48), "RGB", "photo")
    assert np.array_equal(np.asarray(editor.apply_filter(img, "invert", 0)), np.asarray(img))
    half = arr(editor.apply_filter(img, "invert", 0.5))
    assert np.abs(half - 127.5).max() <= 1


def test_known_filter_maths():
    img = Image.new("RGB", (8, 8), (200, 100, 50))
    assert editor.apply_filter(img, "invert").getpixel((0, 0)) == (55, 155, 205)
    r, g, b = editor.apply_filter(img, "mono").getpixel((0, 0))
    assert r == g == b == round(0.2126 * 200 + 0.7152 * 100 + 0.0722 * 50)
    r, g, b = editor.apply_filter(img, "sepia").getpixel((0, 0))
    assert r > g > b  # warm brown


def test_css_filters_match_the_browser_matrices():
    # kodachrome = sepia(.2) saturate(1.45) contrast(1.15) hue-rotate(-5deg), as in static/js/catalog.js
    img = Image.new("RGB", (4, 4), (120, 160, 90))
    a = np.array([[[120, 160, 90]]], np.float32) / 255
    expected = editor._css(a, (("sepia", 0.2), ("saturate", 1.45), ("contrast", 1.15), ("hue-rotate", -5)))
    got = np.array(editor.apply_filter(img, "kodachrome").getpixel((0, 0)))
    assert np.abs(got - np.round(expected[0, 0] * 255)).max() <= 1


def test_filter_aliases_and_errors():
    img = Image.new("RGB", (4, 4), RED)
    assert editor.apply_filter(img, "grayscale").getpixel((0, 0)) == editor.apply_filter(img, "mono").getpixel((0, 0))
    assert editor.apply_filter(img, "none").getpixel((0, 0)) == RED
    with pytest.raises(ValueError, match="Unknown filter"):
        editor.apply_filter(img, "deepdream")
    with pytest.raises(ValueError, match="intensity"):
        editor.apply_filter(img, "mono", 1.5)


def test_filters_work_in_strips_on_tall_images(monkeypatch):
    monkeypatch.setattr(editor, "STRIP_PIXELS", 1000)
    img = make_image((50, 400), "RGB", "photo")
    strips = editor.apply_filter(img, "vintage")
    monkeypatch.setattr(editor, "STRIP_PIXELS", 10**9)
    whole = editor.apply_filter(img, "vintage")
    assert np.abs(arr(strips) - arr(whole)).max() <= 1  # vignette is positional: strips must agree


# ------------------------------------------------------------------ frames
def test_there_are_about_nine_frames():
    assert len(editor.FRAMES) >= 9


def test_line_frame_adds_a_border_of_the_colour():
    img = Image.new("RGB", (200, 100), RED)
    out = editor.apply_frame(img, "line", "#0000ff", 0.1)
    assert out.size == (220, 120) and out.getpixel((2, 2)) == (0, 0, 255) and out.getpixel((110, 60)) == RED


def test_polaroid_has_a_deep_bottom_and_mat_has_a_rule():
    img = Image.new("RGB", (200, 200), RED)
    pol = editor.apply_frame(img, "polaroid", None, 0.05)
    top = next(y for y in range(pol.height) if pol.getpixel((100, y)) == RED)
    bottom = pol.height - 1 - next(y for y in range(pol.height - 1, -1, -1) if pol.getpixel((100, y)) == RED)
    assert bottom > 3 * top
    mat = editor.apply_frame(img, "passe-partout", "#f2ecdf", 0.08)
    assert mat.getpixel((0, 0))[:3] == (0x1D, 0x1B, 0x19)


@pytest.mark.parametrize("style", ["rounded", "circle", "stamp"])
def test_shaped_frames_make_corners_transparent(style):
    out = editor.apply_frame(Image.new("RGB", (200, 160), RED), style, None, 0.08)
    assert out.mode == "RGBA" and out.getpixel((0, 0))[3] == 0
    assert out.getpixel((out.width // 2, out.height // 2))[:3] == RED


def test_vignette_fades_edges_into_the_colour_and_keeps_the_centre():
    img = Image.new("RGB", (300, 200), (200, 200, 200))
    out = editor.apply_frame(img, "vignette", "#000000", 0.05)
    assert out.size == img.size
    assert out.getpixel((150, 100)) == (200, 200, 200)
    assert out.getpixel((0, 100))[0] < 20 and out.getpixel((0, 0))[0] < 20
    assert out.getpixel((150, 25))[0] > out.getpixel((150, 5))[0]


@pytest.mark.parametrize("style", [s for s in editor.FRAMES])
def test_frame_size_is_an_upper_bound(style):
    img = Image.new("RGB", (123, 77), RED)
    out = editor.apply_frame(img, style, None, 0.1)
    bound = editor.frame_size(style, img.size, 0.1)
    assert out.width <= bound[0] and out.height <= bound[1]


def test_frame_errors():
    img = Image.new("RGB", (10, 10))
    with pytest.raises(ValueError, match="Unknown frame"):
        editor.apply_frame(img, "baroque")
    with pytest.raises(ValueError, match="width"):
        editor.apply_frame(img, "line", None, 0.9)


# ------------------------------------------------------------------ overlay
def bbox_of(img: Image.Image, base: Image.Image) -> tuple[int, int, int, int]:
    diff = np.abs(arr(img.convert("RGB")) - arr(base.convert("RGB"))).sum(axis=2)
    ys, xs = np.where(diff > 40)
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def test_shape_box_matches_the_ui_geometry():
    base = Image.new("RGB", (400, 200), (255, 255, 255))
    out = editor.apply_overlay(base, [{"type": "shape", "shape": "rect", "x": 0.5, "y": 0.5, "w": 0.5, "h": 0.5,
                                       "color": "#ff0000", "stroke_width": 0.01}])
    x0, y0, x1, y1 = bbox_of(out, base)
    assert abs(x0 - 100) <= 1 and abs(x1 - 300) <= 1 and abs(y0 - 50) <= 1 and abs(y1 - 150) <= 1
    assert out.getpixel((101, 100))[0] > 200 and out.getpixel((200, 100)) == (255, 255, 255)  # outline only


@pytest.mark.parametrize("shape", ["ellipse", "star", "heart"])
def test_filled_shapes_fill_their_middle(shape):
    base = Image.new("RGB", (200, 200), (255, 255, 255))
    out = editor.apply_overlay(base, [{"type": "shape", "shape": shape, "w": 0.6, "h": 0.6, "color": "#000000",
                                       "fill": "#00ff00", "stroke_width": 0.01}])
    assert out.getpixel((100, 110)) == (0, 255, 0)
    assert out.getpixel((2, 2)) == (255, 255, 255)


@pytest.mark.parametrize("shape", ["line", "arrow"])
def test_lines_ignore_fill_and_rotate_about_their_centre(shape):
    base = Image.new("RGB", (200, 200), (255, 255, 255))
    flat = editor.apply_overlay(base, [{"type": "shape", "shape": shape, "w": 0.8, "h": 0.1, "color": "#000",
                                        "fill": "#00ff00", "stroke_width": 0.02}])
    x0, y0, x1, y1 = bbox_of(flat, base)
    assert x1 - x0 > 140 and y1 - y0 < 30
    turned = editor.apply_overlay(base, [{"type": "shape", "shape": shape, "w": 0.8, "h": 0.1, "color": "#000",
                                          "stroke_width": 0.02, "rotation": 90}])
    x0, y0, x1, y1 = bbox_of(turned, base)
    assert y1 - y0 > 140 and x1 - x0 < 30 and abs((x0 + x1) / 2 - 100) < 6


def test_brush_width_is_a_share_of_the_width_and_follows_points():
    base = Image.new("RGB", (400, 200), (255, 255, 255))
    out = editor.apply_overlay(base, [{"type": "brush", "points": [[0.1, 0.5], [0.9, 0.5]], "color": "#0000ff",
                                       "width": 0.05}])
    x0, y0, x1, y1 = bbox_of(out, base)
    assert abs((y1 - y0) - 20) <= 2 and x0 <= 32 and x1 >= 368  # 0.05 × 400 px, round caps
    dot = editor.apply_overlay(base, [{"type": "brush", "points": [[0.5, 0.5]], "color": "#0000ff", "width": 0.05}])
    assert dot.getpixel((200, 100)) == (0, 0, 255)


def test_highlighter_multiplies():
    base = Image.new("RGB", (100, 100), (200, 200, 200))
    out = editor.apply_overlay(base, [{"type": "brush", "mode": "highlighter", "points": [[0.1, 0.5], [0.9, 0.5]],
                                       "color": "#ffff00", "width": 0.2}])
    r, g, b = out.getpixel((50, 50))
    assert r > 150 and b < 30


def test_sticker_is_contained_in_its_box():
    base = Image.new("RGB", (400, 400), (255, 255, 255))
    out = editor.apply_overlay(base, [{"type": "sticker", "sticker": "openmoji/red-heart", "x": 0.5, "y": 0.5,
                                       "w": 0.5, "h": 0.25}])
    x0, y0, x1, y1 = bbox_of(out, base)
    assert y1 - y0 <= 100 + 2 and x1 - x0 <= 100 + 2    # 1:1 sticker fitted to the 200 × 100 box


def test_text_item_size_is_a_share_of_the_height_and_opacity_applies():
    base = Image.new("RGB", (400, 200), (0, 0, 0))
    out = editor.apply_overlay(base, [{"type": "text", "text": "I", "size": 0.5, "font": "dejavu-sans",
                                       "color": "#ffffff"}])
    x0, y0, x1, y1 = bbox_of(out, base)
    assert 50 <= y1 - y0 <= 100
    faint = editor.apply_overlay(base, [{"type": "text", "text": "I", "size": 0.5, "font": "dejavu-sans",
                                         "color": "#ffffff", "opacity": 0.4}])
    assert 90 <= max(faint.getpixel((x, 100))[0] for x in range(150, 250)) <= 115


def test_items_draw_in_order_and_alpha_images_stay_rgba():
    base = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    items = [{"type": "shape", "shape": "rect", "w": 0.8, "h": 0.8, "color": "#ff0000", "fill": "#ff0000"},
             {"type": "shape", "shape": "rect", "w": 0.4, "h": 0.4, "color": "#0000ff", "fill": "#0000ff"}]
    out = editor.apply_overlay(base, items)
    assert out.mode == "RGBA" and out.getpixel((50, 50)) == (0, 0, 255, 255) and out.getpixel((15, 15))[0] == 255
    assert out.getpixel((2, 2))[3] == 0


def test_items_off_the_canvas_are_clipped():
    base = Image.new("RGB", (100, 100), (255, 255, 255))
    out = editor.apply_overlay(base, [{"type": "shape", "shape": "rect", "x": 1.9, "y": -0.9, "w": 0.5, "h": 0.5,
                                       "fill": "#000"},
                                      {"type": "brush", "points": [[-1, -1], [-0.5, -0.5]], "width": 0.1}])
    assert out.getpixel((50, 50)) == (255, 255, 255)


def test_overlay_limits():
    base = Image.new("RGB", (50, 50))
    with pytest.raises(ValueError, match="Too many overlay items"):
        editor.apply_overlay(base, [{"type": "shape", "shape": "rect"}] * (editor.MAX_ITEMS + 1))
    with pytest.raises(ValueError, match="brush points"):
        editor.apply_overlay(base, [{"type": "brush", "points": [[0.5, 0.5]] * (editor.MAX_BRUSH_POINTS + 1)}])
    with pytest.raises(ValueError, match="too large"):
        editor.apply_overlay(base, [{"type": "shape", "shape": "rect", "w": 1e6, "h": 1e6}])
    with pytest.raises(ValueError, match="Unknown sticker"):
        editor.apply_overlay(base, [{"type": "sticker", "sticker": "../../etc/passwd"}])
    with pytest.raises(ValueError, match="Unknown overlay item"):
        editor.apply_overlay(base, [{"type": "video"}])


# ------------------------------------------------------------------ stickers / catalogue
def test_sticker_set_and_credit():
    stickers = editor.stickers()
    assert len(stickers) >= 120
    openmoji = [s for s in stickers if s["source"] == "openmoji"]
    assert len(openmoji) >= 100 and all(s["url"] == f"/api/assets/stickers/{s['id']}.svg" for s in stickers)
    assert all(0.2 < s["ratio"] < 5 for s in stickers)
    assert "CC BY-SA 4.0" in editor.STICKER_CREDIT
    assert (editor.STICKER_DIR / "openmoji" / "LICENSE-CC-BY-SA-4.0.txt").is_file()
    assert (editor.STICKER_DIR / "ATTRIBUTION.md").is_file()


@pytest.mark.parametrize("sid", ["heyp/smiley", "openmoji/grinning-face"])
def test_stickers_render_crisply_at_any_width(sid):
    small, big = editor.render_sticker(sid, 32), editor.render_sticker(sid, 512)
    assert small.width == 32 and big.width == 512 and big.mode == "RGBA"
    assert np.asarray(big.getchannel("A")).max() == 255


def test_sticker_lookup_never_takes_paths():
    for bad in ("../manifest", "openmoji/../../x", "/etc/passwd", "openmoji/GRINNING"):
        assert not editor.has_sticker(bad)
        with pytest.raises(KeyError):
            editor.sticker_svg(bad)


def test_catalog_lists_everything_the_ui_needs():
    cat = editor.catalog()
    assert {f["id"] for f in cat["filters"]} == set(editor.FILTERS)
    assert all("label" in f for f in cat["filters"] + cat["frames"])
    assert set(cat["shapes"]) == {"rect", "ellipse", "line", "arrow", "star", "heart"}
    assert cat["fonts"] and cat["stickers"] and cat["sticker_credit"]


def test_shape_svg_never_contains_user_text():
    svg = editor.shape_svg("rect", 10, 10, color="red", fill="#00ff0080", stroke=2)
    assert 'stroke="#ff0000"' in svg and 'fill-opacity="0.5020"' in svg and "red" not in svg
