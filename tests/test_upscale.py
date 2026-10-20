"""Tests for AI upscaling (app.imaging.upscale) and the ``upscale`` op / ``img.upscale`` job tool."""
from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont
from pydantic import ValidationError

from app.imaging import io as imgio, pipeline, upscale
from app.imaging.opreg import FeatureUnavailable, PipelineError, get_op
from app.schemas import OPS_ADAPTER

ROOT = Path(__file__).resolve().parents[1]
ASTRONAUT = Path(__file__).parent / "fixtures" / "astronaut.png"
FONT = ROOT / "app" / "assets" / "fonts" / "DejaVuSans-Bold.ttf"

needs_model = pytest.mark.skipif(not (upscale.available("balanced") and upscale.available("strong")),
                                 reason="upscale model not installed")


def parse_op(data: dict):
    return OPS_ADAPTER.validate_python([data])[0]


def psnr(a: Image.Image | np.ndarray, b: Image.Image | np.ndarray) -> float:
    d = np.asarray(a, np.float64) - np.asarray(b, np.float64)
    mse = float((d * d).mean())
    return 99.0 if mse == 0 else 10 * math.log10(255 * 255 / mse)


def text_card(size: int = 256) -> Image.Image:
    """Hard edges (text, a ring) on paper: where a real upscaler must beat bicubic clearly."""
    img = Image.new("RGB", (size, size), (240, 236, 228))
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(str(FONT), 28) if FONT.is_file() else ImageFont.load_default()
    for i in range(6):
        d.text((10, 10 + 40 * i), "heyp :) 0123", fill=(20, 20, 20), font=font)
    d.ellipse((150, 150, 240, 240), outline=(200, 30, 30), width=5)
    return img


def loaded(img: Image.Image, fmt: str = "png") -> imgio.Loaded:
    return imgio.Loaded(image=img, format=fmt, exif=None, icc_profile=None, dpi=None, frames=1, bit_depth=8,
                        orientation=1)


@pytest.fixture(scope="module")
def astronaut() -> Image.Image:
    if not ASTRONAUT.is_file():
        pytest.skip("tests/fixtures/astronaut.png missing")
    return Image.open(ASTRONAUT).convert("RGB")


# ------------------------------------------------------------------ limits (no model needed)
def test_check_limits_input_output_and_factor(monkeypatch):
    monkeypatch.delenv("HEYP_UPSCALE_MAX_INPUT_MP", raising=False)
    upscale.check_limits((2000, 1500), 2, 50_000_000)  # 3 MP → 12 MP: fine
    with pytest.raises(ValueError, match="factor"):
        upscale.check_limits((100, 100), 3, 50_000_000)
    with pytest.raises(upscale.LimitError, match="above the 50 MP limit") as info:
        upscale.check_limits((2000, 1600), 4, 50_000_000)  # 3.2 MP × 16 = 51 MP
    assert info.value.status == 413 and "use 2×" in str(info.value)
    with pytest.raises(upscale.LimitError, match="up to 6.0 MP"):
        upscale.check_limits((4000, 3000), 2, 10**12)  # 12 MP input


@pytest.mark.parametrize("raw, expected", [("2", 2_000_000), ("0.5", 500_000), ("", 6_000_000), ("x", 6_000_000),
                                           ("-1", 6_000_000), ("nan", 6_000_000), ("inf", 6_000_000)])
def test_max_input_env(monkeypatch, raw, expected):
    monkeypatch.setenv("HEYP_UPSCALE_MAX_INPUT_MP", raw)
    assert upscale.max_input_pixels() == expected


def test_grid_tiles_cover_the_image_evenly():
    for n in (1, 2, 255, 256, 257, 511, 513, 1000, 4097):
        count, size = upscale._grid(n)
        assert size <= upscale.TILE and count * size >= n and (count - 1) * size < n


def test_bad_arguments():
    with pytest.raises(TypeError):
        upscale.upscale(np.zeros((4, 4, 3), np.uint8), 2)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="factor"):
        upscale.upscale_rgb(np.zeros((4, 4, 3), np.uint8), 3)
    with pytest.raises(ValueError, match="unknown upscale model"):
        upscale.upscale_rgb(np.zeros((4, 4, 3), np.uint8), 2, "ultra")
    with pytest.raises(ValueError, match="HxWx3 uint8"):
        upscale.upscale_rgb(np.zeros((4, 4), np.uint8), 2)


def test_op_json_is_validated():
    assert parse_op({"op": "upscale"}).model_dump() == {"op": "upscale", "factor": 2, "model": "balanced"}
    for bad in ({"op": "upscale", "factor": 3}, {"op": "upscale", "factor": 8}, {"op": "upscale", "model": "x"},
                {"op": "upscale", "scale": 2}, {"op": "upscale", "factor": "2x"}):
        with pytest.raises(ValidationError):
            parse_op(bad)
    spec = get_op("upscale")
    assert spec.heavy and spec.geometry and spec.frame_apply is None


def test_op_unavailable_is_503(monkeypatch):
    monkeypatch.setattr(upscale, "available", lambda model="balanced": False)
    with pytest.raises(FeatureUnavailable) as info:
        pipeline.run(loaded(Image.new("RGB", (20, 20))), [{"op": "upscale"}])
    assert info.value.status == 503 and "unavailable" in info.value.detail


def test_op_refuses_oversized_input_and_output(monkeypatch):
    monkeypatch.setattr(upscale, "available", lambda model="balanced": True)
    called = []
    monkeypatch.setattr(upscale, "upscale", lambda *a, **k: called.append(1))
    monkeypatch.setenv("HEYP_UPSCALE_MAX_INPUT_MP", "0.001")  # 1000 px
    with pytest.raises(PipelineError) as info:
        pipeline.run(loaded(Image.new("RGB", (40, 30))), [{"op": "upscale"}])
    assert info.value.status == 413 and "Upscaling works on images up to" in info.value.detail
    monkeypatch.delenv("HEYP_UPSCALE_MAX_INPUT_MP")
    with pytest.raises(PipelineError) as info:
        pipeline.run(loaded(Image.new("RGB", (400, 300))), [{"op": "upscale", "factor": 4}], max_pixels=1_000_000)
    assert info.value.status == 413 and "above the 1 MP limit" in info.value.detail
    assert called == []  # refused before any model work


def logo(hidden: tuple[int, int, int], size: tuple[int, int] = (120, 80)) -> Image.Image:
    """An anti-aliased yellow ellipse on transparency, with ``hidden`` stored under every alpha-0 pixel."""
    big = Image.new("RGBA", (size[0] * 4, size[1] * 4), (*hidden, 0))
    ImageDraw.Draw(big).ellipse((size[0] * 0.4, size[1] * 0.4, size[0] * 3.6, size[1] * 3.6), fill=(255, 200, 0, 255))
    a = np.array(big.resize(size, Image.Resampling.LANCZOS))
    a[a[..., 3] == 0, :3] = hidden
    return Image.fromarray(a)


def test_fill_hidden_extends_the_nearest_visible_colour():
    rgb = np.zeros((5, 6, 3), np.uint8)
    rgb[:, :2] = (200, 10, 10)
    rgb[:, 4:] = (10, 10, 200)
    alpha = np.zeros((5, 6), np.uint8)
    alpha[:, :2] = 255
    alpha[:, 4:] = 1  # barely visible still counts as visible: its colour is kept
    out = upscale.fill_hidden(rgb, alpha)
    assert (out[:, :3] == (200, 10, 10)).all() and (out[:, 4:] == (10, 10, 200)).all()
    assert upscale.fill_hidden(rgb, np.full((5, 6), 9, np.uint8)) is rgb  # nothing hidden: untouched
    assert not upscale.fill_hidden(rgb + 7, np.zeros((5, 6), np.uint8)).any()  # all hidden: one fixed colour


def test_hidden_colour_never_reaches_the_network(monkeypatch):
    """img-11: the colour under alpha 0 is invisible, so it must not change what the model sees or returns."""
    seen = []

    def fake(rgb, factor, model="balanced", **_):
        seen.append(rgb.copy())
        return cv2.resize(rgb, (rgb.shape[1] * factor, rgb.shape[0] * factor), interpolation=cv2.INTER_CUBIC)

    monkeypatch.setattr(upscale, "upscale_rgb", fake)
    black, white = upscale.upscale(logo((0, 0, 0)), 2), upscale.upscale(logo((255, 255, 255)), 2)
    assert np.array_equal(seen[0], seen[1])
    assert black.mode == "RGBA" and np.array_equal(np.asarray(black), np.asarray(white))
    la = logo((0, 0, 0)).convert("LA")
    la_white = np.array(la)
    la_white[la_white[..., 1] == 0, 0] = 255
    assert np.array_equal(np.asarray(upscale.upscale(la, 2)), np.asarray(upscale.upscale(Image.fromarray(la_white), 2)))


def test_plan_accounts_for_the_bigger_output():
    p2 = pipeline.plan((1000, 1000), [{"op": "upscale", "factor": 2}])
    p4 = pipeline.plan((1000, 1000), [{"op": "upscale", "factor": 4}])
    assert p2.peak_pixels == 4_000_000 and p4.peak_pixels == 16_000_000
    assert p4.work > p2.work > 20 and p4.peak_bytes > p2.peak_bytes > 250_000_000


# ------------------------------------------------------------------ model-backed
@needs_model
@pytest.mark.parametrize("size", [(1, 1), (1, 7), (7, 1), (31, 17), (257, 300), (600, 140)])
@pytest.mark.parametrize("factor", [2, 4])
def test_output_sizes_are_exact(size, factor):
    rng = np.random.default_rng(1)
    img = Image.fromarray(rng.integers(0, 256, (size[1], size[0], 3), dtype=np.uint8))
    out = upscale.upscale(img, factor)
    assert out.size == (size[0] * factor, size[1] * factor) and out.mode == "RGB"


@needs_model
@pytest.mark.parametrize("mode", ["RGBA", "LA", "L", "P", "CMYK", "I;16"])
def test_modes_and_alpha_are_kept(mode):
    base = Image.new("RGB", (40, 30), (200, 60, 40))
    if mode in ("RGBA", "LA"):
        img = base.convert(mode)
        alpha = Image.new("L", (40, 30), 0)
        alpha.paste(255, (0, 0, 20, 30))
        img.putalpha(alpha)
    elif mode == "I;16":
        img = Image.fromarray((np.arange(1200, dtype=np.uint16).reshape(30, 40) * 50))
    else:
        img = base.convert(mode)
    out = upscale.upscale(img, 2)
    assert out.size == (80, 60)
    if mode in ("RGBA", "LA"):
        assert out.mode == mode
        a = np.asarray(out.getchannel("A"))
        assert a[:, :36].min() > 240 and a[:, 44:].max() < 15  # transparency follows the source
    elif mode == "L":
        assert out.mode == "L"
    else:
        assert out.mode == "RGB"


@needs_model
@pytest.mark.parametrize("factor", [2, 4])
def test_transparent_logo_has_no_fringe_from_hidden_colour(factor):
    """img-11: a logo stored with black under its transparent pixels gave a dark edge on white (R down to ~223)."""
    black, white = upscale.upscale(logo((0, 0, 0)), factor), upscale.upscale(logo((255, 255, 255)), factor)
    assert np.array_equal(np.asarray(black), np.asarray(white))
    on_white = Image.new("RGBA", black.size, (255, 255, 255, 255))
    on_white.alpha_composite(black)
    a = np.asarray(black)[..., 3]
    edge = (a > 0) & (a < 255)
    assert edge.sum() > 100 and np.asarray(on_white)[..., 0][edge].min() > 235  # yellow on white: R stays high


@needs_model
@pytest.mark.parametrize("factor", [2, 4])
def test_beats_bicubic_on_hard_edges(factor):
    gt = text_card()
    small = gt.resize((gt.width // factor, gt.height // factor), Image.Resampling.BICUBIC)
    bicubic = psnr(small.resize(gt.size, Image.Resampling.BICUBIC), gt)
    for model in ("balanced", "strong"):
        assert psnr(upscale.upscale(small, factor, model), gt) >= bicubic + 1.0, model


@needs_model
def test_photo_stays_faithful_and_gets_sharper(astronaut):
    """On a natural photo the result, scaled back down, must match the input (no drift / colour shift) and
    carry more high-frequency detail than bicubic (it is not just a blurry enlargement)."""
    small = astronaut.crop((128, 0, 384, 256)).resize((128, 128), Image.Resampling.LANCZOS)
    up = upscale.upscale(small, 2)
    assert psnr(up.resize(small.size, Image.Resampling.BOX), small) > 30
    means = np.asarray(up, np.float64).mean(axis=(0, 1)) - np.asarray(small, np.float64).mean(axis=(0, 1))
    assert np.abs(means).max() < 3

    def detail(img: Image.Image) -> float:
        g = np.asarray(img.convert("L"), np.float64)
        return float(np.abs(np.diff(g, axis=0)).mean() + np.abs(np.diff(g, axis=1)).mean())

    assert detail(up) > 1.1 * detail(small.resize(up.size, Image.Resampling.BICUBIC))


@needs_model
def test_tiles_have_no_seams(monkeypatch):
    """A 300×300 image in 256-px tiles (2×2 tiles with 16 px context) ≈ the same image in one tile."""
    rng = np.random.default_rng(3)
    arr = np.asarray(text_card(300), np.float32) + rng.normal(0, 4, (300, 300, 3))
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    tiled = upscale.upscale(img, 2)
    monkeypatch.setattr(upscale, "TILE", 512)
    whole = upscale.upscale(img, 2)
    assert psnr(tiled, whole) > 40
    seam = np.abs(np.asarray(tiled, np.float64) - np.asarray(whole, np.float64))[290:310].max()
    assert seam < 40  # nothing like a hard edge along the tile boundary (output row 300)


@needs_model
def test_progress_and_cancel():
    img = Image.new("RGB", (600, 300), (90, 120, 150))
    seen: list[float] = []
    upscale.upscale(img, 2, progress=seen.append)
    assert seen == sorted(seen) and seen[-1] == 1.0 and len(seen) == 6  # 3×2 tiles

    class Stop(Exception):
        pass

    calls = []

    def cancel():
        calls.append(1)
        if len(calls) == 2:
            raise Stop()

    with pytest.raises(Stop):
        upscale.upscale(img, 2, check_cancel=cancel)
    assert len(calls) == 2  # stopped at the second tile


@needs_model
def test_op_runs_in_the_pipeline_with_progress():
    events: list[tuple[float, str]] = []
    ctx = pipeline.run(loaded(Image.new("RGB", (64, 48), (10, 200, 30))), [{"op": "upscale", "factor": 4}],
                       on_progress=lambda f, m: events.append((f, m)))
    assert ctx.image.size == (256, 192)
    assert events and events[-1][0] == 1.0 and "Upscaling 4×" in events[-1][1]
    data, fmt, info = pipeline.render(ctx, {"format": "png"})
    assert (info["width"], info["height"]) == (256, 192) and fmt == "png"


@needs_model
def test_broken_model_file_is_unavailable(tmp_path, monkeypatch):
    """A truncated download fails once with a clear 503 and is not re-read on every request."""
    from app.imaging import ortsession

    (tmp_path / upscale.MODEL_FILES["strong"]).write_bytes(b"\0" * 2_000_000)
    monkeypatch.setenv("HEYP_MODELS_DIR", str(tmp_path))
    session = ortsession.ModelSession(upscale.MODEL_FILES["strong"], min_bytes=1_000_000, label="test")
    assert session.available()
    with pytest.raises(ortsession.ModelUnavailable, match="could not load the test model"):
        session.get()
    assert not session.available()
    with pytest.raises(ortsession.ModelUnavailable):
        session.get()
