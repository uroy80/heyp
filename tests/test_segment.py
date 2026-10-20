"""Tests for app.imaging.segment (U²-Net person segmentation + compositing)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.imaging import segment

ASTRONAUT = Path(__file__).parent / "fixtures" / "astronaut.png"

needs_model = pytest.mark.skipif(not segment.available(), reason="U²-Net model not installed")


@pytest.fixture(scope="module")
def astronaut() -> Image.Image:
    if not ASTRONAUT.is_file():
        pytest.skip("tests/fixtures/astronaut.png missing")
    return Image.open(ASTRONAUT).convert("RGB")


@pytest.fixture(scope="module")
def astro_mask(astronaut) -> np.ndarray:
    if not segment.available():
        pytest.skip("U²-Net model not installed")
    return segment.person_mask(astronaut)


# --- model-backed -----------------------------------------------------------------------


@needs_model
def test_person_mask_on_astronaut(astro_mask):
    m = astro_mask
    assert m.dtype == np.float32 and m.shape == (512, 512)
    assert 0.0 <= m.min() and m.max() <= 1.0
    for corner in (m[:40, :40], m[:40, -40:], m[-40:, -40:]):
        assert corner.mean() < 0.05
    assert m[90:170, 185:260].mean() > 0.95   # face
    assert m[330:450, 80:240].mean() > 0.95   # torso / suit
    assert m[20:120, 400:500].mean() < 0.1    # backdrop right of the head


@needs_model
def test_person_mask_custom_size(astronaut):
    m = segment.person_mask(astronaut, size=(100, 80))
    assert m.shape == (80, 100) and m.dtype == np.float32


@needs_model
def test_remove_background(astronaut):
    out = segment.remove_background(astronaut)
    assert out.mode == "RGBA" and out.size == astronaut.size
    a = np.asarray(out)
    assert a[:20, :20, 3].max() < 20
    assert a[120:150, 200:240, 3].min() > 240
    # Fully transparent pixels carry no trace of the removed background.
    assert not a[a[..., 3] == 0][:, :3].any()


@needs_model
def test_replace_background(astronaut):
    out = segment.replace_background(astronaut, "#0000ff")
    assert out.mode == "RGB"
    assert out.getpixel((5, 5)) == pytest.approx((0, 0, 255), abs=3)
    assert out.getpixel((220, 130)) == astronaut.getpixel((220, 130))
    assert segment.replace_background(astronaut, "transparent").mode == "RGBA"


def test_unavailable_model(monkeypatch, tmp_path):
    monkeypatch.setenv("HEYP_MODELS_DIR", str(tmp_path))
    monkeypatch.setattr(segment, "_session", None)
    monkeypatch.setattr(segment, "_session_failed", False)
    assert segment.available() is False
    with pytest.raises(RuntimeError, match="unavailable"):
        segment.person_mask(Image.new("RGB", (64, 64)))


# --- compositing (synthetic, no model) ----------------------------------------------------


def _split(w: int = 120, h: int = 80) -> tuple[Image.Image, np.ndarray]:
    img = Image.new("RGB", (w, h), (200, 40, 40))
    mask = np.zeros((h, w), np.float32)
    mask[:, : w // 2] = 1.0
    return img, mask


def test_composite_solid_colour():
    img, mask = _split()
    out = segment.composite(img, mask, "#00ff00")
    assert out.mode == "RGB" and out.size == img.size
    assert out.getpixel((5, 40)) == (200, 40, 40)
    assert out.getpixel((115, 40)) == (0, 255, 0)
    # Feathered edge: a blend, not a hard step.
    edge = out.getpixel((60, 40))
    assert edge not in ((200, 40, 40), (0, 255, 0))


def test_composite_no_feather_is_hard_edge():
    img, mask = _split()
    out = np.asarray(segment.composite(img, mask, "white", feather=0))
    assert (out[:, :60] == (200, 40, 40)).all() and (out[:, 60:] == 255).all()


def test_composite_transparent():
    img, mask = _split()
    out = segment.composite(img, mask, "transparent")
    assert out.mode == "RGBA"
    assert out.getpixel((5, 40)) == (200, 40, 40, 255)
    assert out.getpixel((115, 40)) == (0, 0, 0, 0)


def test_composite_semi_transparent_background():
    img, mask = _split()
    out = segment.composite(img, mask, "#0000ff80", feather=0)
    assert out.mode == "RGBA"
    assert out.getpixel((115, 40)) == (0, 0, 255, 128)
    assert out.getpixel((5, 40)) == (200, 40, 40, 255)


def test_composite_respects_existing_alpha():
    img, mask = _split()
    rgba = img.convert("RGBA")
    rgba.putpixel((5, 5), (200, 40, 40, 0))
    out = segment.composite(rgba, mask, "#ffffff", feather=0)
    assert out.getpixel((5, 5)) == (255, 255, 255)
    assert out.getpixel((5, 40)) == (200, 40, 40)


def test_composite_validates_mask_shape():
    img, mask = _split()
    with pytest.raises(ValueError, match="does not match"):
        segment.composite(img, mask[:10], "white")
    with pytest.raises(ValueError):
        segment.composite(img, mask, "not-a-colour")


def test_decontamination_reduces_background_spill():
    """Edge pixels mixed with green background should not stay green over a white backdrop."""
    w = h = 200
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    dist = np.hypot(xx - 100, yy - 100)
    alpha = np.clip((70 - dist) / 12.0, 0.0, 1.0)  # disc with a 12 px soft edge
    fg, old_bg, new_bg = np.array([200, 30, 30.0]), np.array([30, 200, 30.0]), np.array([255, 255, 255.0])
    a = alpha[..., None]
    img = Image.fromarray(np.clip(a * fg + (1 - a) * old_bg + 0.5, 0, 255).astype(np.uint8))
    ideal = a * fg + (1 - a) * new_bg
    edge = (alpha > 0.05) & (alpha < 0.95)

    def err(decon: bool) -> float:
        out = np.asarray(segment.composite(img, alpha, "white", feather=0, decontaminate=decon), np.float32)
        return float(np.abs(out - ideal)[edge].mean())

    assert err(True) < 0.5 * err(False)


@pytest.mark.parametrize("spec,expected", [
    ("#fff", (255, 255, 255, 255)),
    ("#dbe9f7", (219, 233, 247, 255)),
    ("White", (255, 255, 255, 255)),
    ("transparent", (0, 0, 0, 0)),
    ("#11223380", (17, 34, 51, 128)),
    ((1, 2, 3), (1, 2, 3, 255)),
    (None, (255, 255, 255, 255)),
])
def test_parse_color(spec, expected):
    assert segment.parse_color(spec) == expected


@pytest.mark.parametrize("bad", ["#12", "blurple", (1, 2), (0, 0, 300)])
def test_parse_color_invalid(bad):
    with pytest.raises(ValueError):
        segment.parse_color(bad)
