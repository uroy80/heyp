"""Pipeline: op order, DPI tracking, output-format rules, memoisation, guards."""
from __future__ import annotations

import time
from io import BytesIO

import numpy as np
import pytest
from PIL import Image

from app.config import get_settings
from app.imaging import face, io as imgio, passport, pipeline, segment
from app.schemas import OutputSpec
from app.store import FileStore

RED, BLUE, GREEN, YELLOW = (255, 0, 0), (0, 0, 255), (0, 255, 0), (255, 255, 0)
PIL_FORMAT = {"jpeg": "JPEG", "png": "PNG", "webp": "WEBP", "ico": "ICO", "tiff": "TIFF", "bmp": "BMP"}


@pytest.fixture(autouse=True)
def _register_and_reset():
    imgio.register_plugins()
    pipeline.clear_caches()
    yield
    pipeline.clear_caches()
    get_settings.cache_clear()
    imgio.register_plugins()  # monkeypatched env is restored by now; reset the Pillow pixel limit


def quadrants(w: int = 200, h: int = 100) -> Image.Image:
    """TL red, TR blue, BL green, BR yellow."""
    img = Image.new("RGB", (w, h), RED)
    img.paste(BLUE, (w // 2, 0, w, h // 2))
    img.paste(GREEN, (0, h // 2, w // 2, h))
    img.paste(YELLOW, (w // 2, h // 2, w, h))
    return img


def load(img: Image.Image, fmt: str = "png", **save) -> imgio.Loaded:
    buf = BytesIO()
    img.save(buf, format=PIL_FORMAT[fmt], **save)
    return imgio.load(buf.getvalue())


def near(px, color, tol: int = 40) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(px[:3], color))


def out(**kw) -> OutputSpec:
    return OutputSpec(**kw)


def test_rotate_is_clockwise_then_crop_sees_rotated_image():
    loaded = load(quadrants())
    ctx = pipeline.run(loaded, [{"op": "rotate", "angle": 90},
                                {"op": "crop", "x": 0, "y": 0, "width": 50, "height": 50}])
    assert ctx.image.size == (50, 50)
    # clockwise: the original bottom-left (green) quadrant ends up top-left
    assert near(ctx.image.convert("RGB").getpixel((25, 25)), GREEN)


def test_op_order_matters():
    loaded = load(quadrants())
    a = pipeline.run(loaded, [{"op": "rotate", "angle": 90},
                              {"op": "crop", "x": 0, "y": 0, "width": 100, "height": 50}])
    b = pipeline.run(loaded, [{"op": "crop", "x": 0, "y": 0, "width": 100, "height": 50},
                              {"op": "rotate", "angle": 90}])
    assert a.image.size == (100, 50)
    assert b.image.size == (50, 100)
    assert near(b.image.convert("RGB").getpixel((25, 50)), RED)


def test_source_image_is_not_mutated():
    loaded = load(quadrants())
    before = loaded.image.tobytes()
    pipeline.run(loaded, [{"op": "rotate", "angle": 33, "fill": "transparent"}, {"op": "flip", "direction": "vertical"},
                          {"op": "adjust", "brightness": 1.5, "grayscale": True},
                          {"op": "pad", "top": 5, "left": 5}, {"op": "resize", "mode": "scale", "scale": 50}])
    assert loaded.image.tobytes() == before


def test_print_size_resize_sets_pipeline_dpi():
    loaded = load(quadrants(600, 300))
    ctx = pipeline.run(loaded, [{"op": "resize", "mode": "fit", "width": 2, "unit": "in", "dpi": 150}])
    assert ctx.image.size == (300, 150)
    assert ctx.dpi == (150.0, 150.0)
    data, fmt, info = pipeline.render(ctx, out(format="jpeg"))
    assert fmt == "jpeg" and info["dpi"] == [150, 150]
    assert (info["width"], info["height"]) == (300, 150)
    with Image.open(BytesIO(data)) as im:
        assert im.format == "JPEG" and im.size == (300, 150)
        assert round(im.info["dpi"][0]) == 150


def test_pixel_resize_keeps_source_dpi_and_output_dpi_overrides():
    loaded = load(quadrants(), "jpeg", dpi=(72, 72))
    ctx = pipeline.run(loaded, [{"op": "resize", "mode": "scale", "scale": 50}])
    assert ctx.dpi is not None and round(ctx.dpi[0]) == 72
    _, _, info = pipeline.render(ctx, out(format="png", dpi=300))
    assert info["dpi"] == [300, 300]


def test_dpi_is_null_for_formats_without_resolution():
    ctx = pipeline.run(load(quadrants()), [])
    _, fmt, info = pipeline.render(ctx, out(format="webp", dpi=300))
    assert fmt == "webp" and info["dpi"] is None


@pytest.mark.parametrize(("src", "mode", "expected"), [
    ("png", "RGB", "png"), ("jpeg", "RGB", "jpeg"), ("webp", "RGB", "webp"), ("tiff", "RGB", "tiff"),
    ("ico", "RGB", "jpeg"), ("ico", "RGBA", "png"),
])
def test_original_format_rule(src, mode, expected):
    img = Image.new(mode, (64, 64), (10, 20, 30, 0) if mode == "RGBA" else (10, 20, 30))
    loaded = load(img, src)
    data, fmt, _ = pipeline.render(pipeline.run(loaded, []), out(format="original"))
    assert fmt == expected
    with Image.open(BytesIO(data)) as im:
        assert im.format == PIL_FORMAT[expected]


def test_original_jpeg_with_new_transparency_becomes_png():
    rgba = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    assert pipeline.resolve_format("original", "jpeg", rgba) == "png"
    assert pipeline.resolve_format("original", "jpeg", Image.new("RGBA", (10, 10), (0, 0, 0, 255))) == "jpeg"
    assert pipeline.resolve_format("original", "webp", rgba) == "webp"
    assert pipeline.resolve_format("png", "jpeg", Image.new("RGB", (4, 4))) == "png"


def test_target_size_uses_compressor():
    rng = np.random.default_rng(1)
    noisy = Image.fromarray(rng.integers(0, 255, (700, 700, 3), dtype=np.uint8), "RGB")
    ctx = pipeline.run(load(noisy), [])
    data, fmt, info = pipeline.render(ctx, out(format="jpeg", quality=80, max_kb=40))
    assert fmt == "jpeg" and len(data) <= 40 * 1024
    with Image.open(BytesIO(data)) as im:
        assert (info["width"], info["height"]) == im.size


def test_target_size_for_pdf():
    rng = np.random.default_rng(2)
    noisy = Image.fromarray(rng.integers(0, 255, (600, 600, 3), dtype=np.uint8), "RGB")
    ctx = pipeline.run(load(noisy), [])
    data, fmt, info = pipeline.render(ctx, out(format="pdf", max_kb=60))
    assert fmt == "pdf" and data.startswith(b"%PDF") and len(data) <= 60 * 1024


def test_pdf_reports_the_dpi_it_is_written_at():
    ctx = pipeline.run(load(quadrants()), [])
    assert ctx.dpi is None
    assert pipeline.render(ctx, out(format="pdf"))[2]["dpi"] == [300, 300]
    assert pipeline.render(ctx, out(format="pdf", dpi=150))[2]["dpi"] == [150, 150]
    assert pipeline.render(ctx, out(format="webp", dpi=150))[2]["dpi"] is None


def test_target_size_quality_is_a_ceiling():
    ctx = pipeline.run(load(quadrants(400, 400)), [])
    capped, _, _ = pipeline.render(ctx, out(format="jpeg", quality=30, max_kb=5000))
    plain, _, _ = pipeline.render(ctx, out(format="jpeg", quality=30))
    assert capped == plain


def test_render_accepts_dict_and_none():
    ctx = pipeline.run(load(quadrants()), [])
    assert pipeline.render(ctx, {"format": "png"})[1] == "png"
    assert pipeline.render(ctx)[1] == "jpeg"


def test_dict_ops_are_validated():
    loaded = load(quadrants())
    assert pipeline.run(loaded, [{"op": "flip", "direction": "horizontal"}]).image.size == (200, 100)
    with pytest.raises(ValueError, match="invalid op"):
        pipeline.run(loaded, [{"op": "flip"}])
    with pytest.raises(ValueError):
        pipeline.run(loaded, [{"op": "explode"}])


def test_pixel_guard(monkeypatch):
    monkeypatch.setenv("HEYP_MAX_PIXELS", "50000")
    get_settings.cache_clear()
    loaded = load(quadrants())
    with pytest.raises(pipeline.PipelineError) as info:
        pipeline.run(loaded, [{"op": "resize", "mode": "scale", "scale": 1000}])
    assert info.value.status == 413
    with pytest.raises(pipeline.PipelineError):
        pipeline.run(loaded, [{"op": "pad", "top": 500, "bottom": 500}])
    # never enlarging is always allowed
    ctx = pipeline.run(loaded, [{"op": "resize", "mode": "scale", "scale": 1000, "upscale": False}])
    assert ctx.image.size == (200, 100)


def test_background_unavailable_is_503(monkeypatch):
    monkeypatch.setattr(segment, "available", lambda: False)
    with pytest.raises(pipeline.FeatureUnavailable) as info:
        pipeline.run(load(quadrants()), [{"op": "background", "mode": "remove"}])
    assert info.value.status == 503 and "unavailable" in info.value.detail


def test_background_mask_is_memoised_per_image_and_prefix(monkeypatch):
    calls: list[tuple[int, int]] = []

    def fake_mask(img, **_kw):
        calls.append(img.size)
        mask = np.zeros((img.height, img.width), np.float32)
        mask[:, : img.width // 2] = 1.0
        return mask

    monkeypatch.setattr(segment, "available", lambda: True)
    monkeypatch.setattr(segment, "person_mask", fake_mask)
    loaded = load(quadrants())
    ops = [{"op": "background", "mode": "remove"}]
    first = pipeline.run(loaded, ops, cache_key="k1")
    second = pipeline.run(loaded, [{"op": "background", "mode": "replace", "color": "#00ff00"}], cache_key="k1")
    assert len(calls) == 1
    assert first.image.mode == "RGBA" and first.image.getpixel((190, 50))[3] == 0
    assert second.image.mode == "RGB" and near(second.image.getpixel((190, 50)), GREEN)
    pipeline.run(loaded, [{"op": "flip", "direction": "horizontal"}] + ops, cache_key="k1")
    assert len(calls) == 2  # different preceding ops → new analysis
    pipeline.run(loaded, ops)  # no cache key → never memoised
    assert len(calls) == 3
    pipeline.forget("k1")
    pipeline.run(loaded, ops, cache_key="k1")
    assert len(calls) == 4


def test_crop_aspect_face_falls_back_with_warning(monkeypatch):
    monkeypatch.setattr(face, "detect", lambda img, **kw: [])
    ctx = pipeline.run(load(quadrants()), [{"op": "crop_aspect", "ratio": "1:1", "anchor": "face"}])
    assert ctx.image.size == (100, 100)
    assert any("No face" in w for w in ctx.warnings)


def test_passport_uses_preset_memoises_analysis_and_sets_dpi(monkeypatch):
    analyses: list[tuple[int, int]] = []
    seen: dict = {}

    def fake_analyze(img, *a, **kw):
        analyses.append(img.size)
        return {"faces": []}

    def fake_make(img, preset, **kw):
        seen.update(kw, preset=preset)
        w, h = preset.output_size()
        return Image.new("RGB", (w, h), "white"), {"dpi": preset.dpi, "face_found": False,
                                                   "warnings": ["No face found"]}

    monkeypatch.setattr(passport, "analyze", fake_analyze)
    monkeypatch.setattr(passport, "make", fake_make)
    loaded = load(quadrants())
    ops = [{"op": "passport", "preset": "us", "zoom": 1.1, "background": "#dbe9f7"}]
    ctx = pipeline.run(loaded, ops, cache_key="p1")
    pipeline.run(loaded, [{"op": "passport", "preset": "us", "zoom": 0.9}], cache_key="p1")
    assert len(analyses) == 1
    assert ctx.image.size == (600, 600) and ctx.dpi == (300.0, 300.0)
    assert ctx.warnings == ["No face found"]
    assert ctx.meta["passport_preset"] == "us"
    assert seen["preset"].id == "us" and seen["zoom"] == 0.9
    _, _, info = pipeline.render(ctx, out(format="jpeg"))
    assert info["dpi"] == [300, 300] and info["warnings"] == ["No face found"]


def test_passport_custom_and_unknown_preset(monkeypatch):
    monkeypatch.setattr(passport, "analyze", lambda img, *a, **kw: {})
    monkeypatch.setattr(passport, "make", lambda img, preset, **kw: (
        Image.new("RGB", preset.output_size()), {"dpi": preset.dpi, "warnings": []}))
    loaded = load(quadrants())
    ctx = pipeline.run(loaded, [{"op": "passport", "preset": "custom",
                                 "custom": {"width_mm": 25.4, "height_mm": 50.8, "dpi": 100}}])
    assert ctx.image.size == (100, 200) and ctx.dpi == (100.0, 100.0)
    assert ctx.meta["passport_preset"] == "custom"
    with pytest.raises(ValueError, match="unknown passport preset"):
        pipeline.run(loaded, [{"op": "passport", "preset": "atlantis"}])


def test_sheet_gets_pipeline_dpi(monkeypatch):
    seen: dict = {}

    def fake_sheet(img, **kw):
        seen.update(kw)
        return Image.new("RGB", (1200, 1800), "white"), {"copies": 6, "warnings": ["Only 6 copies fit"]}

    monkeypatch.setattr(passport, "sheet", fake_sheet)
    loaded = load(quadrants(400, 400))
    ctx = pipeline.run(loaded, [{"op": "resize", "mode": "fit", "width": 2, "unit": "in", "dpi": 200},
                                {"op": "sheet", "paper": "4x6", "dpi": 300, "copies": 9}])
    assert seen["src_dpi"] == 200 and seen["paper"] == "4x6" and seen["copies"] == 9
    assert ctx.dpi == (300.0, 300.0) and ctx.meta["sheet_paper"] == "4x6"
    assert ctx.warnings == ["Only 6 copies fit"]
    with pytest.raises(ValueError, match="unknown paper"):
        pipeline.run(loaded, [{"op": "sheet", "paper": "a0"}])


def test_trim_and_crop_aspect_center():
    img = Image.new("RGB", (300, 200), "white")
    img.paste((200, 30, 30), (100, 50, 200, 150))
    ctx = pipeline.run(load(img), [{"op": "trim"}, {"op": "crop_aspect", "ratio": "2:1"}])
    assert ctx.image.size == (100, 50)


# ------------------------------------------------------------------ v1.1: sheet dpi (VIS-4 / C5)
def _fake_make(info_extra: dict | None = None):
    def make(img, preset, **kw):
        w, h = preset.output_size()
        info = {"dpi": preset.dpi, "face_found": False, "head_ratio": None, "eye_from_bottom": None,
                "face_box_out": None, "warnings": []}
        info.update(info_extra or {})
        make.kwargs = kw
        return Image.new("RGB", (w, h), "white"), info
    return make


def test_sheet_without_dpi_keeps_the_presets_print_resolution(monkeypatch):
    monkeypatch.setattr(passport, "analyze", lambda img, *a, **kw: {})
    monkeypatch.setattr(passport, "make", _fake_make())
    ctx = pipeline.run(load(quadrants(400, 500)), [{"op": "passport", "preset": "nl"},
                                                   {"op": "sheet", "paper": "4x6", "cut_lines": True}])
    assert ctx.meta["sheet"]["dpi"] == 400 and ctx.meta["sheet"]["photo_px"] == [551, 709]
    assert ctx.dpi == (400.0, 400.0)
    assert sorted(ctx.image.size) == [1600, 2400]
    _, _, info = pipeline.render(ctx, out(format="jpeg"))
    assert info["dpi"] == [400, 400]


@pytest.mark.parametrize(("src_dpi", "explicit", "expected"), [
    (None, None, 300), (72, None, 300), (150, None, 300), (400, None, 400), (457, None, 457),
    (1200, None, 600), (72, 200, 200), (400, 300, 300),
])
def test_sheet_dpi_resolution(monkeypatch, src_dpi, explicit, expected):
    seen: dict = {}

    def fake_sheet(img, **kw):
        seen.update(kw)
        return Image.new("RGB", (10, 10), "white"), {"dpi": kw["dpi"], "warnings": []}

    monkeypatch.setattr(passport, "sheet", fake_sheet)
    loaded = load(quadrants(), "jpeg", dpi=(src_dpi, src_dpi)) if src_dpi else load(quadrants())
    op = {"op": "sheet", "paper": "4x6"} | ({"dpi": explicit} if explicit else {})
    ctx = pipeline.run(loaded, [op])
    assert seen["dpi"] == expected and ctx.dpi == (float(expected), float(expected))


# ------------------------------------------------------------------ v1.1: passport guides (VIS-9 / C2), level (C4)
FACE_INFO = {"face_found": True, "head_ratio": 0.594, "eye_from_bottom": 0.62, "roll_deg": 3.2,
             "face_box_out": {"box": [200, 100, 200, 260], "crown_y": 52.0, "chin_y": 409.0, "eye_y": 228.0,
                              "center_x": 300.0, "top_y": 41.5}}


def test_passport_guides_shape(monkeypatch):
    make = _fake_make(FACE_INFO)
    monkeypatch.setattr(passport, "analyze", lambda img, *a, **kw: {})
    monkeypatch.setattr(passport, "make", make)
    ctx = pipeline.run(load(quadrants()), [{"op": "passport", "preset": "us", "level": False}])
    assert make.kwargs["level"] is False
    guides = pipeline.passport_guides(ctx)
    assert guides == {
        "preset": "us", "width": 600, "height": 600, "head_ratio": 0.594, "eye_from_bottom": 0.62,
        "face_found": True, "roll_deg": 3.2, "lines": {"crown": 52.0, "chin": 409.0, "eye": 228.0},
        "hair_top": 41.5,
        "bands": {"head_min": 0.5, "head_max": 0.6875, "eye_min": 0.5625, "eye_max": 0.6875,
                  "top_min": None, "top_max": None},
        "background_edit_allowed": guides["background_edit_allowed"], "source": None,
    }
    assert isinstance(guides["background_edit_allowed"], bool)
    # without a hair top in the face box (older make() info) the field is null, never a guess
    no_hair = {**FACE_INFO, "face_box_out": {k: v for k, v in FACE_INFO["face_box_out"].items() if k != "top_y"}}
    monkeypatch.setattr(passport, "make", _fake_make(no_hair))
    assert pipeline.passport_guides(pipeline.run(load(quadrants()), [{"op": "passport", "preset": "us"}]))[
        "hair_top"] is None


def test_passport_guides_carry_the_source_frame(monkeypatch):
    """Context view: ``source`` is make()'s source_frame as plain JSON numbers; malformed → None."""
    frame = {"width": np.int64(400), "height": 500, "center": (np.float64(210.5), 260.25),
             "size": [300.0, 300.0], "angle": np.float32(2.5)}
    monkeypatch.setattr(passport, "analyze", lambda img, *a, **kw: {})
    monkeypatch.setattr(passport, "make", _fake_make({**FACE_INFO, "source_frame": frame}))
    guides = pipeline.passport_guides(pipeline.run(load(quadrants()), [{"op": "passport", "preset": "us"}]))
    assert guides["source"] == {"width": 400, "height": 500, "center": [210.5, 260.25], "size": [300.0, 300.0],
                                "angle": 2.5}
    assert all(type(v) is float for v in guides["source"]["center"] + guides["source"]["size"])
    assert type(guides["source"]["width"]) is int and type(guides["source"]["angle"]) is float
    for bad in ({"width": 1}, {**frame, "center": [1.0]}, {**frame, "size": None}, "frame"):
        monkeypatch.setattr(passport, "make", _fake_make({**FACE_INFO, "source_frame": bad}))
        assert pipeline.passport_guides(pipeline.run(load(quadrants()), [{"op": "passport", "preset": "us"}]))[
            "source"] is None


def test_passport_source_frame_refers_to_the_image_entering_the_op(monkeypatch):
    """Real make(): the source size is the result of the preceding ops (here rotated 90° → 100×200)."""
    monkeypatch.setattr(passport, "analyze", lambda img, *a, **kw: passport.Analysis([], None, img.size))
    ops = [{"op": "rotate", "angle": 90}, {"op": "passport", "preset": "us", "zoom": 1.25, "offset_y": 0.1}]
    ctx = pipeline.run(load(quadrants(200, 100)), ops)
    source = pipeline.passport_guides(ctx)["source"]
    # Centred square crop of the 100×200 image, 80 px at zoom 1.25, subject moved down by 0.1 · 80 px.
    assert source == {"width": 100, "height": 200, "center": [50.0, 92.0], "size": [80.0, 80.0], "angle": 0.0}
    assert ctx.meta["passport"]["source_frame"] == source
    kept = pipeline.run(load(quadrants(200, 100)), [*ops, {"op": "adjust", "brightness": 1.2}])
    assert pipeline.passport_guides(kept)["source"] == source


def test_passport_guides_without_face_have_no_lines(monkeypatch):
    monkeypatch.setattr(passport, "analyze", lambda img, *a, **kw: {})
    monkeypatch.setattr(passport, "make", _fake_make())
    guides = pipeline.passport_guides(pipeline.run(load(quadrants()), [{"op": "passport", "preset": "us"}]))
    assert guides is not None and guides["face_found"] is False and guides["lines"] is None
    assert guides["hair_top"] is None
    assert pipeline.passport_guides(pipeline.run(load(quadrants()), [])) is None


@pytest.mark.parametrize(("after", "kept"), [
    ({"op": "adjust", "brightness": 1.1}, True),
    ({"op": "sheet", "paper": "4x6"}, False),
    ({"op": "resize", "mode": "scale", "scale": 50}, False),
    ({"op": "crop", "x": 0, "y": 0, "width": 100, "height": 100}, False),
    ({"op": "rotate", "angle": 90}, False),
    ({"op": "pad", "top": 4}, False),
    ({"op": "flip", "direction": "vertical"}, False),
    ({"op": "trim"}, False),
])
def test_passport_guides_dropped_after_geometry_change(monkeypatch, after, kept):
    monkeypatch.setattr(passport, "analyze", lambda img, *a, **kw: {})
    monkeypatch.setattr(passport, "make", _fake_make(FACE_INFO))
    ctx = pipeline.run(load(quadrants()), [{"op": "passport", "preset": "us"}, after])
    assert (pipeline.passport_guides(ctx) is not None) is kept
    # a later passport op defines the frame again
    again = pipeline.run(load(quadrants()), [{"op": "passport", "preset": "us"}, after,
                                             {"op": "passport", "preset": "uk"}])
    assert pipeline.passport_guides(again)["preset"] == "uk"


# ------------------------------------------------------------------ v1.1: encode info (C2)
def test_render_reports_encode_info_only_for_target_size():
    ctx = pipeline.run(load(quadrants(300, 300)), [])
    assert pipeline.render(ctx, out(format="jpeg"))[2]["encode"] is None
    _, _, info = pipeline.render(ctx, out(format="jpeg", quality=80, max_kb=500))
    assert info["encode"] == {"quality": 80, "scale": 1.0, "met": True}


# ------------------------------------------------------------------ memory bounds (DEL-1) / expiry (VIS-11)
def test_mask_cache_is_bounded_by_bytes(monkeypatch):
    monkeypatch.setattr(segment, "available", lambda: True)
    monkeypatch.setattr(segment, "person_mask", lambda img, **kw: np.ones((img.height, img.width), np.float32))
    monkeypatch.setattr(pipeline, "_mask_cache", pipeline._LRU(3, max_bytes=50_000))
    loaded = load(quadrants(200, 100))  # float16 mask = 40 000 B
    for prefix in ([], [{"op": "flip", "direction": "horizontal"}], [{"op": "flip", "direction": "vertical"}]):
        pipeline.run(loaded, prefix + [{"op": "background", "mode": "remove"}], cache_key="m")
    assert len(pipeline._mask_cache.keys()) == 1 and pipeline._mask_cache.nbytes == 40_000


def test_lru_byte_accounting():
    lru = pipeline._LRU(4, max_bytes=1000)
    lru.put("a|1", np.zeros(100, np.uint8))
    lru.put("a|2", np.zeros(300, np.uint8))
    lru.put("b|1", np.zeros(700, np.uint8))
    assert lru.keys() == ["a|2", "b|1"] and lru.nbytes == 1000
    lru.put("b|1", np.zeros(5000, np.uint8))  # API-15: an entry above the whole budget is not stored
    assert lru.keys() == ["a|2"] and lru.nbytes == 300
    lru.drop_prefix("a|")
    assert lru.keys() == [] and lru.nbytes == 0


def test_expired_uploads_drop_analysis_and_masks(tmp_path, monkeypatch):
    monkeypatch.setattr(segment, "available", lambda: True)
    monkeypatch.setattr(segment, "person_mask", lambda img, **kw: np.ones((img.height, img.width), np.float32))
    monkeypatch.setattr(passport, "analyze", lambda img, *a, **kw: {"faces": []})
    monkeypatch.setattr(passport, "make", _fake_make())
    store = FileStore(tmp_path / "s", ttl=60, on_remove=pipeline.forget)
    store.init()
    buf = BytesIO()
    quadrants().save(buf, "PNG")
    saved = store.save("a.png", BytesIO(buf.getvalue()), max_bytes=10**6)
    pipeline.run(store.load(saved.id), [{"op": "background", "mode": "remove"},
                                        {"op": "passport", "preset": "us"}], cache_key=saved.id)
    assert pipeline._mask_cache.keys() and pipeline._analysis_cache.keys()
    assert store.cleanup_expired(now=time.time() + 3600) == [saved.id]
    assert not any(k.startswith(saved.id + "|") for k in pipeline._mask_cache.keys())
    assert not any(k.startswith(saved.id + "|") for k in pipeline._analysis_cache.keys())


# ------------------------------------------------------------------ robustness (API-2, API-3, API-11, API-14, API-16)
def test_max_pixels_argument_overrides_the_environment():
    loaded = load(quadrants(200, 100))
    with pytest.raises(pipeline.PipelineError, match="above the 0 MP limit"):
        pipeline.run(loaded, [{"op": "resize", "mode": "scale", "scale": 300}], max_pixels=100_000)
    with pytest.raises(pipeline.PipelineError) as info:
        pipeline.run(loaded, [], max_pixels=10_000)  # the source itself is checked too
    assert info.value.status == 413 and "Image is" in info.value.detail
    assert pipeline.run(loaded, [{"op": "resize", "mode": "scale", "scale": 300}]).image.size == (600, 300)


def test_heavy_op_and_encoder_caps(monkeypatch):
    monkeypatch.setattr(segment, "available", lambda: True)
    monkeypatch.setattr(segment, "person_mask", lambda img, **kw: np.ones((img.height, img.width), np.float32))
    monkeypatch.setitem(pipeline.OP_MAX_PIXELS, "background", 10_000)
    loaded = load(quadrants(200, 100))
    with pytest.raises(pipeline.PipelineError, match="Background removal works on images up to") as info:
        pipeline.run(loaded, [{"op": "background", "mode": "remove"}])
    assert info.value.status == 413
    assert pipeline.run(loaded, [{"op": "crop", "x": 0, "y": 0, "width": 100, "height": 100},
                                 {"op": "background", "mode": "remove"}]).image.size == (100, 100)
    monkeypatch.setitem(pipeline.LOSSLESS_ENCODE_MAX_PIXELS, "webp", 10_000)
    ctx = pipeline.run(loaded, [])
    with pytest.raises(pipeline.PipelineError, match="Lossless WEBP output is limited to"):
        pipeline.render(ctx, out(format="webp", lossless=True))
    assert pipeline.render(ctx, out(format="webp"))[1] == "webp"  # lossy WebP is not capped


def test_tiny_target_size_is_rounded_up_to_one_byte():
    ctx = pipeline.run(load(quadrants(64, 64)), [])
    data, _, info = pipeline.render(ctx, out(format="jpeg", max_kb=0.0001))
    assert data and info["encode"]["met"] is False and info["warnings"]
    assert (pipeline.kb_to_bytes(None), pipeline.kb_to_bytes(0.0001), pipeline.kb_to_bytes(2)) == (None, 1, 2048)


def test_memo_put_is_undone_when_the_upload_was_deleted(monkeypatch):
    monkeypatch.setattr(segment, "available", lambda: True)
    monkeypatch.setattr(segment, "person_mask", lambda img, **kw: np.ones((img.height, img.width), np.float32))
    ops = [{"op": "background", "mode": "remove"}]
    pipeline.run(load(quadrants()), ops, cache_key="gone", alive=lambda: False)
    assert pipeline._mask_cache.keys() == []
    pipeline.run(load(quadrants()), ops, cache_key="here", alive=lambda: True)
    assert [k.split("|")[0] for k in pipeline._mask_cache.keys()] == ["here"]


def test_plan_sizes_follow_the_ops():
    p = pipeline.plan((4000, 3000), [], out(format="jpeg"))
    assert p.peak_pixels == 12_000_000 and p.work == pytest.approx(12 * (0.03 + 0.03))
    up = pipeline.plan((100, 100), [{"op": "resize", "mode": "exact", "width": 5000, "height": 4000},
                                    {"op": "crop", "x": 0, "y": 0, "width": 0.5, "height": 0.5, "unit": "fraction"}])
    assert up.peak_pixels == 20_000_000 and up.peak_bytes >= 4 * 20_000_000
    passport_plan = pipeline.plan((4000, 3000), [{"op": "passport", "preset": "us"}, {"op": "sheet", "paper": "4x6"}])
    assert passport_plan.peak_pixels == 12_000_000  # source; the 4×6 sheet at the preset's 300 dpi is 2.2 MP
    sheet_only = pipeline.plan((100, 100), [{"op": "sheet", "paper": "a4"}])
    assert sheet_only.peak_pixels == pytest.approx(210 * 297 / 25.4 ** 2 * 600 ** 2, rel=0.001)  # unknown dpi → 600
    rotated = pipeline.plan((1000, 10), [{"op": "rotate", "angle": 90}, {"op": "rotate", "angle": 45}])
    assert rotated.peak_pixels == pytest.approx((1010 / 2 ** 0.5) ** 2, rel=0.01)
    with pytest.raises(ValueError, match="atlantis"):
        pipeline.plan((10, 10), [{"op": "passport", "preset": "atlantis"}])


def test_plan_charges_slow_encoders_and_target_size():
    jpeg = pipeline.plan((5000, 4000), [], out(format="jpeg")).work
    heic = pipeline.plan((5000, 4000), [], out(format="heic")).work
    heic_kb = pipeline.plan((5000, 4000), [], out(format="heic", max_kb=200)).work
    webp_lossless = pipeline.plan((5000, 4000), [], out(format="webp", lossless=True))
    assert jpeg < heic < heic_kb and heic_kb > 4 * heic - 1
    assert webp_lossless.peak_bytes > 30 * 20_000_000
    original = pipeline.plan((5000, 4000), [], out(format="original"), source_format="heic").work
    assert original == pytest.approx(heic)
    bg = pipeline.plan((5000, 4000), [{"op": "background", "mode": "replace"}], out(format="jpeg"))
    assert bg.peak_bytes > 30 * 20_000_000


def test_passport_guides_prefer_the_lines_and_bands_make_reports(monkeypatch):
    reported = {**FACE_INFO, "lines": {"crown": 60.0, "chin": 400.0, "eye": 230.0},
                "bands": {"head_min": 0.5, "head_max": 0.69, "eye_min": None, "eye_max": None,
                          "top_min": 0.1, "top_max": 0.2},
                "background_edit_allowed": False}
    monkeypatch.setattr(passport, "analyze", lambda img, *a, **kw: {})
    monkeypatch.setattr(passport, "make", _fake_make(reported))
    guides = pipeline.passport_guides(pipeline.run(load(quadrants()), [{"op": "passport", "preset": "us"}]))
    assert guides["lines"] == {"crown": 60.0, "chin": 400.0, "eye": 230.0}
    assert guides["bands"]["top_min"] == 0.1 and guides["bands"]["eye_min"] is None
    assert guides["background_edit_allowed"] is False
