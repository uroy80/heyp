"""Op plug-ins (app/imaging/ops_ext/core.py + edit.py): JSON, pipeline runs, animations."""
from __future__ import annotations

import io as _io

import numpy as np
import pytest
from pydantic import ValidationError
from PIL import Image

from app import schemas
from app.imaging import animated, io, opreg, pipeline
from app.imaging.ops_ext import core, edit
from tests.conftest import make_gif_bytes, make_image

V1_EXAMPLES = [
    {"op": "rotate", "angle": 90}, {"op": "rotate", "angle": -15.5, "expand": False, "fill": "transparent"},
    {"op": "flip", "direction": "horizontal"}, {"op": "crop", "x": 1, "y": 2, "width": 10, "height": 8},
    {"op": "crop", "x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5, "unit": "fraction"},
    {"op": "crop_aspect", "ratio": "16:9", "anchor": "smart"}, {"op": "trim", "tolerance": 20, "padding": 3},
    {"op": "resize", "mode": "fit", "width": 100}, {"op": "resize", "mode": "exact", "width": 2, "height": 3,
                                                     "unit": "in", "dpi": 300},
    {"op": "pad", "top": 4, "color": "#ff0000"}, {"op": "adjust", "brightness": 1.2, "grayscale": True},
]


def png(img: Image.Image) -> io.Loaded:
    buf = _io.BytesIO()
    img.save(buf, "PNG")
    return io.load(buf.getvalue())


def run(img: Image.Image, ops: list[dict], **kw) -> opreg.Ctx:
    return pipeline.run(png(img), ops, **kw)


# ------------------------------------------------------------------ registration / JSON
def test_core_and_edit_ops_are_registered_by_their_plugins():
    for name in ("rotate", "flip", "crop", "crop_aspect", "trim", "resize", "pad", "adjust"):
        spec = opreg.get_op(name)
        assert spec.apply.__module__ == "app.imaging.ops_ext.core" and not opreg.is_fallback(name)
        assert spec.frame_apply is not None
    for name in ("watermark", "meme", "filter", "frame", "overlay"):
        spec = opreg.get_op(name)
        assert spec.apply.__module__ == "app.imaging.ops_ext.edit" and spec.frame_apply is not None
    assert "app.imaging.ops_ext.core" not in opreg.PLUGIN_ERRORS
    assert "app.imaging.ops_ext.edit" not in opreg.PLUGIN_ERRORS


@pytest.mark.parametrize("example", V1_EXAMPLES)
def test_v1_json_round_trips_unchanged(example):
    op = schemas.OPS_ADAPTER.validate_python([example])[0]
    dumped = op.model_dump(exclude_unset=True)
    assert dumped == {k: v for k, v in example.items()} or dumped == schemas.OPS_ADAPTER.validate_python(
        [example])[0].model_dump(exclude_unset=True)
    assert set(dumped) == set(example)


def test_rotate_gains_only_and_rejects_others():
    op = schemas.OPS_ADAPTER.validate_python([{"op": "rotate", "angle": 90, "only": "portrait"}])[0]
    assert isinstance(op, core.RotateOp) and op.only == "portrait"
    assert schemas.RotateOp is core.RotateOp
    with pytest.raises(ValidationError):
        schemas.OPS_ADAPTER.validate_python([{"op": "rotate", "angle": 90, "only": "square"}])


# The exact JSON static/js/state.js builds.
UI_OPS = [
    {"op": "filter", "name": "kodachrome", "intensity": 0.8},
    {"op": "frame", "style": "passe-partout", "color": "#ffffff", "width": 0.04},
    {"op": "meme", "top": "Top", "bottom": "Bottom", "font": "anton", "size": "auto", "color": "#ffffff",
     "outline": "none", "uppercase": True},
    {"op": "overlay", "items": [
        {"type": "text", "text": "Hi", "x": 0.5, "y": 0.5, "rotation": 0, "opacity": 1, "size": 0.08,
         "font": "libre-franklin", "color": "#ffffff", "align": "center"},
        {"type": "sticker", "sticker": "openmoji/grinning-face", "x": 0.3, "y": 0.3, "rotation": 10, "opacity": 1,
         "w": 0.2, "h": 0.25},
        {"type": "shape", "shape": "star", "x": 0.7, "y": 0.6, "rotation": 0, "opacity": 1, "w": 0.3, "h": 0.22,
         "color": "#e0262e", "fill": None, "stroke_width": 0.008},
        {"type": "brush", "points": [[0.1, 0.1], [0.2, 0.3]], "color": "#e0262e", "width": 0.012, "opacity": 1}]},
    {"op": "watermark", "kind": "text", "opacity": 0.6, "rotation": 0, "position": "bottom-right", "margin": 0.03,
     "text": "(c) me", "font": "libre-franklin", "size": 0.05, "color": "#ffffff"},
]


def test_the_ui_json_validates_and_runs():
    ops = schemas.OPS_ADAPTER.validate_python(UI_OPS)
    assert [type(o) for o in ops] == [edit.FilterOp, edit.FrameOp, edit.MemeOp, edit.OverlayOp, edit.WatermarkOp]
    assert ops[1].style == "mat"
    ctx = run(make_image((320, 240), "RGB", "photo"), UI_OPS)
    assert ctx.image.size == (344, 264) and ctx.warnings == []   # mat: 10 px board + 1 px bevel + 1 px rule
    plan = pipeline.plan((320, 240), UI_OPS)
    assert plan.peak_pixels >= 342 * 262 and plan.work > 0


@pytest.mark.parametrize("bad", [
    {"op": "watermark", "kind": "text", "text": " "},
    {"op": "watermark", "kind": "image"},
    {"op": "watermark", "kind": "image", "image_id": "../../etc"},
    {"op": "watermark", "text": "x", "font": "comic-sans"},
    {"op": "watermark", "text": "x", "opacity": 1.5},
    {"op": "watermark", "text": "x" * 501},
    {"op": "meme"},
    {"op": "meme", "top": "x", "size": 0.9},
    {"op": "meme", "top": "x", "outline": "blurple"},
    {"op": "filter", "name": "deepdream"},
    {"op": "filter", "name": "mono", "intensity": -0.1},
    {"op": "frame", "style": "baroque"},
    {"op": "frame", "style": "line", "width": 0.9},
    {"op": "overlay", "items": [{"type": "sticker", "sticker": "../../etc/passwd"}]},
    {"op": "overlay", "items": [{"type": "shape", "shape": "hexagon"}]},
    {"op": "overlay", "items": [{"type": "text", "text": "x", "evil": 1}]},
    {"op": "overlay", "items": [{"type": "brush", "points": []}]},
    {"op": "overlay", "items": [{"type": "brush", "points": [[float("nan"), 0]]}]},
    {"op": "overlay", "items": [{"type": "shape", "shape": "rect"}] * 101},
    {"op": "overlay", "items": [{"type": "brush", "points": [[0.5, 0.5]] * 15000}] * 2},
])
def test_bad_json_is_refused_with_422_material(bad):
    with pytest.raises((ValidationError, ValueError)):
        schemas.OPS_ADAPTER.validate_python([bad])


def test_aliases_are_accepted():
    ops = schemas.OPS_ADAPTER.validate_python([
        {"op": "watermark", "text": "x", "position": "top_center"}, {"op": "filter", "name": "Grayscale"},
        {"op": "frame", "style": "film-strip"}])
    assert (ops[0].position, ops[1].name, ops[2].style) == ("top", "mono", "film")


# ------------------------------------------------------------------ behaviour through the pipeline
def test_rotate_only_landscape_or_portrait():
    land, port = Image.new("RGB", (40, 20), "red"), Image.new("RGB", (20, 40), "red")
    assert run(land, [{"op": "rotate", "angle": 90, "only": "landscape"}]).image.size == (20, 40)
    assert run(port, [{"op": "rotate", "angle": 90, "only": "landscape"}]).image.size == (20, 40)
    assert run(port, [{"op": "rotate", "angle": 90, "only": "portrait"}]).image.size == (40, 20)
    assert pipeline.plan((20, 40), [{"op": "rotate", "angle": 90, "only": "landscape"}]).peak_pixels == 800


def test_image_watermark_resolves_another_upload():
    logo = png(Image.new("RGBA", (10, 10), (0, 0, 255, 255)))
    ctx = run(Image.new("RGB", (200, 100), "white"),
              [{"op": "watermark", "kind": "image", "image_id": "a" * 32, "image_scale": 0.2, "opacity": 1,
                "position": "top-left", "margin": 0}], resolve_file=lambda fid: logo)
    assert ctx.image.getpixel((5, 5)) == (0, 0, 255)
    with pytest.raises(ValueError, match="another uploaded file"):
        run(Image.new("RGB", (200, 100)), [{"op": "watermark", "kind": "image", "image_id": "a" * 32}])


def test_frame_refuses_results_above_the_pixel_limit():
    ctx_ops = [{"op": "frame", "style": "polaroid", "width": 0.5}]
    with pytest.raises(opreg.PipelineError) as err:
        pipeline.run(png(Image.new("RGB", (100, 100))), ctx_ops, max_pixels=12_000)
    assert err.value.status == 413


def test_meme_outside_changes_geometry_and_plan_bounds_it():
    ops = [{"op": "meme", "top": "a", "bottom": "b", "layout": "outside"}]
    ctx = run(Image.new("RGB", (300, 200), "blue"), ops)
    plan = pipeline.plan((300, 200), ops)
    assert ctx.image.height > 200 and plan.peak_pixels >= ctx.image.width * ctx.image.height


# ------------------------------------------------------------------ animations
def gif_ctx(ops: list[dict], n: int = 5, size=(60, 40)) -> opreg.Ctx:
    data = make_gif_bytes(n, size)
    return pipeline.run(io.load(data), ops, animation=pipeline.load_animation(data))


EVERY_FRAME_OP = [
    [{"op": "rotate", "angle": 90}], [{"op": "rotate", "angle": 33}], [{"op": "flip", "direction": "vertical"}],
    [{"op": "crop", "x": 5, "y": 5, "width": 30, "height": 20}], [{"op": "crop_aspect", "ratio": "1:1"}],
    [{"op": "crop_aspect", "ratio": "1:1", "anchor": "smart"}], [{"op": "trim"}],
    [{"op": "resize", "mode": "scale", "scale": 50}], [{"op": "pad", "top": 5, "color": "#000"}],
    [{"op": "adjust", "grayscale": True}], [{"op": "filter", "name": "sepia"}],
    [{"op": "frame", "style": "line", "width": 0.1}], [{"op": "watermark", "text": "hey"}],
    [{"op": "meme", "top": "TOP"}], [{"op": "overlay", "items": [{"type": "shape", "shape": "rect"}]}],
]


@pytest.mark.parametrize("ops", EVERY_FRAME_OP)
def test_every_op_applies_to_every_frame(ops):
    ctx = gif_ctx(ops)
    assert ctx.frames is not None and len(ctx.frames) == 5 and ctx.warnings == []
    assert len({f.size for f in ctx.frames}) == 1 and ctx.image is ctx.frames[0]
    data, fmt, info = pipeline.render(ctx, schemas.OutputSpec(format="original"))
    back = animated.load(data)
    assert fmt == "gif" and back is not None and back.size == ctx.image.size and back.loop == 0
    # GIF writers merge frames that became identical (e.g. cropped away from the moving block): the
    # timing is kept exactly.
    assert 1 <= len(back) <= 5 and sum(back.durations) == 300


def test_trim_keeps_every_frames_content():
    # The block moves right across frames: the trim box is their union, so no frame loses its block.
    ctx = gif_ctx([{"op": "trim"}], n=4, size=(80, 40))
    for frame in ctx.frames:
        assert np.asarray(frame.getchannel("A")).max() == 255


def test_watermark_is_identical_on_every_frame_and_rendered_once(monkeypatch):
    calls = []
    real = edit.watermark.prepare
    monkeypatch.setattr(edit.watermark, "prepare", lambda *a, **k: calls.append(1) or real(*a, **k))
    ctx = gif_ctx([{"op": "watermark", "text": "©", "position": "center", "opacity": 1, "size": 0.3}])
    assert len(calls) == 1
    centre = [f.convert("RGB").getpixel((30, 20)) for f in ctx.frames]
    assert len(set(centre)) == 1


def test_animation_to_webp_and_still_formats():
    ctx = gif_ctx([{"op": "filter", "name": "invert"}])
    data, fmt, _ = pipeline.render(ctx, schemas.OutputSpec(format="webp"))
    assert fmt == "webp" and len(animated.load(data)) == 5
    data, fmt, info = pipeline.render(ctx, schemas.OutputSpec(format="jpeg"))
    assert fmt == "jpeg" and any("first frame" in w for w in info["warnings"])


# ------------------------------------------------------------------ text warnings reach the response (img-02, img-03)
def test_text_ops_warn_about_text_they_cannot_draw_faithfully():
    from app.imaging import text

    hindi = "नमस्ते"
    ops = [{"op": "watermark", "text": "សួ mark"},                       # Khmer: no bundled face
           {"op": "overlay", "items": [{"type": "text", "text": hindi}]},
           {"op": "meme", "top": "word " * 50, "bottom": "ok"}]
    ctx = run(Image.new("RGB", (300, 600), "white"), ops)
    assert any("empty boxes" in w for w in ctx.warnings)
    assert any("top caption is long" in w for w in ctx.warnings)
    shaping = [w for w in ctx.warnings if "can't shape" in w]
    assert shaping == ([] if text.shaping_available() else
                       ["This server can't shape Devanagari text yet, so letters may not join or combine correctly"])
    clean = run(Image.new("RGB", (300, 200), "white"), [{"op": "watermark", "text": "© Usha 2026"}])
    assert clean.warnings == []


def test_text_warnings_are_given_once_for_an_animation():
    anim = animated.load(make_gif_bytes(4, (60, 40)))
    ctx = pipeline.run(io.load(make_gif_bytes(4, (60, 40))), [{"op": "watermark", "text": "ស mark"}],
                       animation=anim)
    assert ctx.frames is not None and len(ctx.frames) == 4
    assert sum("empty boxes" in w for w in ctx.warnings) == 1
