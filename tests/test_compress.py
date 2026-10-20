"""app.imaging.compress.encode_to_target: size targets for lossy and lossless formats."""
from __future__ import annotations

import io as stdio

import numpy as np
import pytest
from PIL import Image

from app.imaging import compress, io
from tests.conftest import make_image

io.register_plugins()


def _decoded_size(data: bytes) -> tuple[int, int]:
    with Image.open(stdio.BytesIO(data)) as im:
        return im.size


def _check_info(data: bytes, info: dict) -> None:
    assert set(info) >= {"quality", "scale", "iterations", "met", "warning"}
    assert info["size_bytes"] == len(data)
    assert _decoded_size(data) == (info["width"], info["height"])
    assert 0 < info["scale"] <= 1.0
    assert info["iterations"] >= 1


@pytest.mark.parametrize("fmt", ["jpeg", "webp"])
@pytest.mark.parametrize("kb", [20, 50, 200])
def test_lossy_targets_met_on_noisy_photo(noisy_photo, fmt, kb):
    data, info = compress.encode_to_target(noisy_photo, fmt, max_bytes=kb * 1024, quality=90)
    assert len(data) <= kb * 1024
    assert info["met"] is True
    assert 5 <= info["quality"] <= 90
    _check_info(data, info)
    assert io.load(data).format == fmt
    # A quality search never needs more than 8 encodes per round, plus up to 6 downscale rounds.
    assert info["iterations"] <= 8 * 7


def test_lossy_uses_requested_quality_when_it_fits(noisy_photo):
    data, info = compress.encode_to_target(noisy_photo, "jpeg", max_bytes=50 * 1024 * 1024, quality=85)
    assert info == {**info, "quality": 85, "scale": 1.0, "iterations": 1, "met": True, "warning": None}
    assert data == io.save(noisy_photo, "jpeg", quality=85)


@pytest.mark.parametrize("fmt", ["jpeg", "webp"])
def test_downscale_keeps_usable_quality_and_uses_the_budget(noisy_photo, fmt):
    data, info = compress.encode_to_target(noisy_photo, fmt, max_bytes=8 * 1024)
    assert info["met"] and len(data) <= 8 * 1024
    assert info["scale"] < 1.0
    assert "Downscaled" in info["warning"]
    assert info["quality"] >= 30          # not the blocky quality-5 fallback
    assert len(data) >= 0.7 * 8 * 1024    # scale bisection does not grossly undershoot
    _check_info(data, info)


@pytest.mark.parametrize("fmt", ["avif", "heic"])
def test_other_lossy_formats(fmt):
    img = make_image((800, 600), kind="photo", seed=2)
    data, info = compress.encode_to_target(img, fmt, max_bytes=30 * 1024)
    assert len(data) <= 30 * 1024 and info["met"]
    _check_info(data, info)


def test_no_target_is_a_plain_save():
    img = make_image((200, 100), kind="photo")
    data, info = compress.encode_to_target(img, "jpeg", quality=77)
    assert data == io.save(img, "jpeg", quality=77)
    assert info["met"] and info["quality"] == 77 and info["iterations"] == 1


def test_png_path_meets_target_and_reports_no_quality(noisy_photo):
    for kb in (50, 200):
        data, info = compress.encode_to_target(noisy_photo, "png", max_bytes=kb * 1024)
        assert len(data) <= kb * 1024 and info["met"]
        assert info["quality"] is None
        _check_info(data, info)
        assert io.load(data).format == "png"
        # The bisection keeps the image reasonably large instead of grossly undershooting.
        assert len(data) > kb * 1024 * 0.4


def test_png_palette_step_without_downscaling():
    img = make_image((600, 400), kind="photo")
    prepared = io._prepare(img, "png")
    palette_256 = len(io._encode(prepared, colors=256, optimize=True))
    assert len(io._encode(prepared, optimize=True)) > palette_256
    target = palette_256 + 1
    data, info = compress.encode_to_target(img, "png", max_bytes=target)
    assert len(data) <= target and info["met"] and info["scale"] == 1.0
    with Image.open(stdio.BytesIO(data)) as im:
        assert im.mode == "P"


def test_png_with_alpha_keeps_alpha():
    img = make_image((400, 300), "RGBA", kind="photo")
    data, info = compress.encode_to_target(img, "png", max_bytes=60 * 1024)
    assert info["met"]
    assert io.has_alpha(io.load(data).image)


@pytest.mark.parametrize("fmt", ["gif", "bmp", "tiff"])
def test_other_lossless_formats(fmt):
    img = make_image((300, 200), kind="photo")
    target = len(io.save(img, fmt)) // 3
    data, info = compress.encode_to_target(img, fmt, max_bytes=target)
    assert len(data) <= target and info["met"]
    _check_info(data, info)


def test_min_kb_warning_path():
    img = Image.new("RGB", (64, 64), (200, 30, 30))
    data, info = compress.encode_to_target(img, "jpeg", min_bytes=500 * 1024)
    assert info["met"] is False
    assert "minimum" in info["warning"]
    assert len(data) < 500 * 1024  # never padded with junk
    assert info["quality"] >= 90
    data, info = compress.encode_to_target(img, "png", min_bytes=500 * 1024)
    assert info["met"] is False and "minimum" in info["warning"]


def test_min_kb_reached_by_raising_quality(noisy_photo):
    data, info = compress.encode_to_target(noisy_photo, "jpeg", quality=30, min_bytes=700 * 1024,
                                           max_bytes=1500 * 1024)
    assert 700 * 1024 <= len(data) <= 1500 * 1024
    assert info["met"] and info["quality"] > 30


def test_noisy_high_quality_progressive_jpeg_encodes():
    """Pillow's progressive-JPEG buffer guess (w×h bytes) is too small for noise at q≥90, 4:4:4."""
    img = make_image((400, 300), kind="noise")
    data = io.save(img, "jpeg", quality=92, progressive=True)
    assert len(data) > 400 * 300
    assert io.load(data).image.size == (400, 300)


def test_unreachable_target_returns_smallest_with_warning():
    img = make_image((400, 300), kind="noise")
    data, info = compress.encode_to_target(img, "jpeg", max_bytes=200)
    assert info["met"] is False
    assert "Could not compress" in info["warning"]
    assert min(_decoded_size(data)) >= 16
    _check_info(data, info)


def test_lossless_webp_falls_back_to_lossy(noisy_photo):
    data, info = compress.encode_to_target(noisy_photo, "webp", max_bytes=100 * 1024, lossless=True)
    assert len(data) <= 100 * 1024 and info["met"]
    assert "lossy" in info["warning"]
    small = make_image((64, 48), kind="gradient")
    data, info = compress.encode_to_target(small, "webp", max_bytes=1024 * 1024, lossless=True)
    assert info["quality"] is None and info["met"]


def test_metadata_options_pass_through():
    from tests.conftest import make_jpeg_bytes

    loaded = io.load(make_jpeg_bytes((400, 300), gps=True, camera=True, kind="photo"))
    data, info = compress.encode_to_target(loaded.image, "jpeg", max_bytes=20 * 1024, exif=loaded.exif,
                                           strip_metadata=False, dpi=(300, 300))
    out = io.load(data)
    assert out.dpi == (300.0, 300.0)
    exif = Image.Exif()
    exif.load(out.exif)
    assert exif[0x010F] == "Apple" and 0x8825 not in exif


@pytest.mark.parametrize("kwargs", [{"max_bytes": 0}, {"max_bytes": -5}, {"min_bytes": "x"},
                                    {"max_bytes": 100, "min_bytes": 200}, {"quality": 0}])
def test_invalid_arguments(kwargs):
    with pytest.raises(ValueError):
        compress.encode_to_target(make_image(), "jpeg", **kwargs)


def test_unknown_kwarg_and_format():
    with pytest.raises(TypeError):
        compress.encode_to_target(make_image(), "jpeg", max_bytes=1000, colours=8)
    with pytest.raises(ValueError):
        compress.encode_to_target(make_image(), "ico", max_bytes=1000)


# ------------------------------------------------------------------ bounded work (IMG-4 / API-3)
def _record_encodes(monkeypatch) -> list[tuple[tuple[int, int], int | None, bool]]:
    """Patch io._encode to log (image size, quality, lossless) of every encode."""
    log: list[tuple[tuple[int, int], int | None, bool]] = []
    original = io._encode

    def wrapped(prepared, **kwargs):
        log.append((prepared.image.size, kwargs.get("quality"), bool(kwargs.get("lossless"))))
        return original(prepared, **kwargs)

    monkeypatch.setattr(io, "_encode", wrapped)
    return log


def test_hopeless_full_size_search_goes_straight_to_downscaling(monkeypatch):
    img = make_image((800, 600), kind="noise", seed=4)
    log = _record_encodes(monkeypatch)
    data, info = compress.encode_to_target(img, "webp", max_bytes=20 * 1024, quality=90)
    assert info["met"] and info["scale"] < 1.0 and len(data) <= 20 * 1024
    full = [q for size, q, _ in log if size == (800, 600)]
    assert full == [90, 47, 5]  # requested quality, the middle, the floor — not a bisection down to 5


def test_search_down_finds_the_same_quality_as_an_exhaustive_scan():
    img = make_image((400, 300), kind="photo", seed=5)
    prepared = io._prepare(img, "jpeg")
    sizes = {q: len(io._encode(prepared, quality=q)) for q in range(5, 91)}
    for target in (sizes[12] + 10, sizes[47], sizes[60] + 1, sizes[88]):
        _, info = compress.encode_to_target(img, "jpeg", max_bytes=target, quality=90)
        assert info["quality"] == max(q for q, n in sizes.items() if n <= target)


@pytest.fixture
def small_proxy(monkeypatch):
    """Exercise the large-image (proxy-guided) path on small images."""
    monkeypatch.setattr(compress, "PROXY_ABOVE", 300_000)
    monkeypatch.setattr(compress, "PROXY_PIXELS", 60_000)


@pytest.mark.parametrize("fmt,kb", [("jpeg", 60), ("webp", 40), ("avif", 30), ("heic", 30), ("jpeg", 200)])
def test_large_image_search_runs_on_a_proxy(small_proxy, monkeypatch, fmt, kb):
    img = make_image((960, 720), kind="photo", seed=6)
    log = _record_encodes(monkeypatch)
    data, info = compress.encode_to_target(img, fmt, max_bytes=kb * 1024, quality=90)
    _check_info(data, info)
    assert info["met"] and len(data) <= kb * 1024
    assert info["scale"] == 1.0 and 5 <= info["quality"] <= 90
    full = [entry for entry in log if entry[0] == (960, 720)]
    assert 1 <= len(full) <= compress.MAX_LARGE_ENCODES
    assert len(data) >= 0.6 * kb * 1024  # the budget is used, not wasted (noisy content is adversarial)
    assert info["iterations"] == len(log) <= compress.MAX_ENCODES


@pytest.mark.parametrize("fmt", ["jpeg", "webp"])
def test_large_image_downscale_path_on_a_proxy(small_proxy, monkeypatch, fmt):
    img = make_image((960, 720), kind="noise", seed=7)
    log = _record_encodes(monkeypatch)
    data, info = compress.encode_to_target(img, fmt, max_bytes=12 * 1024, quality=90)
    _check_info(data, info)
    assert info["met"] and info["scale"] < 1.0 and "Downscaled" in info["warning"]
    assert len([e for e in log if e[0] == (960, 720)]) <= 3
    assert info["quality"] >= 30 and len(data) >= 0.6 * 12 * 1024


def test_large_image_min_bytes_on_a_proxy(small_proxy, monkeypatch):
    img = make_image((960, 720), kind="photo", seed=8)
    high = len(io.save(img, "jpeg", quality=85))
    log = _record_encodes(monkeypatch)
    data, info = compress.encode_to_target(img, "jpeg", quality=30, min_bytes=high, max_bytes=2 * high)
    assert info["met"] and high <= len(data) <= 2 * high and info["quality"] > 30
    assert len([e for e in log if e[0] == (960, 720)]) <= 1 + compress.MAX_LARGE_ENCODES


def test_large_lossless_skips_hopeless_full_size_encode(small_proxy, monkeypatch):
    img = make_image((960, 720), kind="noise", seed=9)
    log = _record_encodes(monkeypatch)
    data, info = compress.encode_to_target(img, "webp", max_bytes=50 * 1024, lossless=True)
    assert info["met"] and "lossy" in info["warning"]
    assert not any(lossless for size, _, lossless in log if size == (960, 720))


def test_hard_cap_on_encodes(monkeypatch):
    monkeypatch.setattr(compress, "MAX_ENCODES", 4)
    img = make_image((400, 300), kind="noise", seed=10)
    data, info = compress.encode_to_target(img, "jpeg", max_bytes=3 * 1024, quality=90)
    assert info["iterations"] == 4
    assert "stopped" in info["warning"]
    assert len(data) == info["size_bytes"]


def test_large_webp_uses_the_fast_method(monkeypatch):
    methods: list[int] = []
    original = Image.Image.save

    def save(self, fp, format=None, **params):  # noqa: A002 — mirrors Image.save
        if format == "WEBP":
            methods.append(params.get("method"))
        return original(self, fp, format, **params)

    monkeypatch.setattr(Image.Image, "save", save)
    monkeypatch.setattr(io, "_WEBP_FAST_ABOVE", 10_000)
    io.save(make_image((200, 100), kind="photo"), "webp", quality=90)
    io.save(make_image((80, 60), kind="photo"), "webp", quality=90)
    io.save(make_image((200, 100), kind="photo"), "webp", lossless=True)
    assert methods == [2, 4, 4]


def test_large_webp_falls_back_when_the_fast_method_fails(monkeypatch):
    original = Image.Image.save

    def save(self, fp, format=None, **params):  # noqa: A002
        if format == "WEBP" and params.get("method") == 2:
            raise ValueError("encoding error 6")
        return original(self, fp, format, **params)

    monkeypatch.setattr(Image.Image, "save", save)
    monkeypatch.setattr(io, "_WEBP_FAST_ABOVE", 10_000)
    data = io.save(make_image((200, 100), kind="photo"), "webp", quality=90)
    assert io.load(data).image.size == (200, 100)


# ------------------------------------------------------------------ alpha through the palette steps (IMG-5)
@pytest.mark.parametrize("fmt", ["png", "tiff"])
def test_palette_steps_keep_exact_transparency(fmt):
    from tests.conftest import make_cutout

    cut = make_cutout((320, 240))
    src = np.asarray(cut.getchannel("A"))
    prepared = io._prepare(cut, fmt)
    for colors in compress.PALETTE_STEPS:
        target = len(io._encode(prepared, colors=colors, optimize=True))
        data, info = compress.encode_to_target(cut, fmt, max_bytes=target)
        assert info["met"] and info["scale"] == 1.0
        alpha = np.asarray(io.load(data).image.convert("RGBA").getchannel("A"))
        assert (alpha[src == 0] == 0).all(), (fmt, colors)
        assert (alpha[src == 255] == 255).all(), (fmt, colors)
