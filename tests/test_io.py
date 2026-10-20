"""app.imaging.io: decoding, orientation, normalisation, colour, metadata and every output format."""
from __future__ import annotations

import io as stdio
import re
import threading

import numpy as np
import pytest
from PIL import Image, ImageCms, ImageOps, JpegImagePlugin

from app import config
from app.imaging import io
from tests.conftest import (QUADRANT_COLORS, close_color, encode, make_exif_bytes, make_heic_bytes, make_image,
                            make_jpeg_bytes, quadrant_colors)

io.register_plugins()

# Expected corner colours of the quadrant test image after each EXIF orientation is applied.
_ORIENTED_SIZE = {1: (300, 200), 2: (300, 200), 3: (300, 200), 4: (300, 200),
                  5: (200, 300), 6: (200, 300), 7: (200, 300), 8: (200, 300)}


def _exif_of(data: bytes) -> Image.Exif:
    with Image.open(stdio.BytesIO(data)) as im:
        return im.getexif()


def _assert_quadrants(img: Image.Image, expected: Image.Image, tol: int = 16) -> None:
    got, want = quadrant_colors(img), quadrant_colors(expected)
    for corner in got:
        assert close_color(got[corner], want[corner], tol), (corner, got, want)


# ------------------------------------------------------------------ setup & names
def test_register_plugins_is_idempotent_and_reports_features():
    first = io.register_plugins()
    second = io.register_plugins()
    assert first == second == {"heic": True, "avif": True}
    assert io.FEATURES == {"heic": True, "avif": True}
    assert Image.MAX_IMAGE_PIXELS == io.max_pixels() == io.DEFAULT_MAX_PIXELS == config.DEFAULT_MAX_PIXELS == 50_000_000


def test_pixel_limit_comes_from_env(pixel_limit):
    pixel_limit(12345)
    assert io.max_pixels() == 12345
    assert Image.MAX_IMAGE_PIXELS == 12345


@pytest.mark.parametrize("name,canon,mime,ext", [
    ("JPG", "jpeg", "image/jpeg", "jpg"), (".jpeg", "jpeg", "image/jpeg", "jpg"), ("image/jpeg", "jpeg", "image/jpeg", "jpg"),
    ("HEIF", "heic", "image/heic", "heic"), ("hif", "heic", "image/heic", "heic"), ("tif", "tiff", "image/tiff", "tiff"),
    ("png", "png", "image/png", "png"), ("webp", "webp", "image/webp", "webp"), ("avif", "avif", "image/avif", "avif"),
    ("bmp", "bmp", "image/bmp", "bmp"), ("gif", "gif", "image/gif", "gif"), ("pdf", "pdf", "application/pdf", "pdf"),
    ("ico", "ico", "image/vnd.microsoft.icon", "ico"), ("j2k", "jp2", "image/jp2", "jp2"),
])
def test_format_names(name, canon, mime, ext):
    assert io.canonical_format(name) == canon
    assert io.mime_for(name) == mime
    assert io.ext_for(name) == ext


@pytest.mark.parametrize("bad", ["", "xyz", "original", None, "image/x-foo"])
def test_unknown_format_names_raise(bad):
    with pytest.raises(ValueError):
        io.canonical_format(bad)


# ------------------------------------------------------------------ orientation
@pytest.mark.parametrize("orientation", range(1, 9))
def test_jpeg_every_exif_orientation_applied_once(orientation):
    data = make_jpeg_bytes((300, 200), orientation=orientation)
    loaded = io.load(data)
    assert loaded.image.size == _ORIENTED_SIZE[orientation]
    assert loaded.orientation == orientation
    with Image.open(stdio.BytesIO(data)) as raw:
        reference = ImageOps.exif_transpose(raw)
    _assert_quadrants(loaded.image, reference)
    if loaded.exif is not None:
        assert _exif_of_bytes(loaded.exif).get(0x0112, 1) == 1


def test_jpeg_orientation_6_of_300x200_loads_as_200x300():
    loaded = io.load(make_jpeg_bytes((300, 200), orientation=6))
    assert loaded.image.size == (200, 300)
    # 90° clockwise: the original bottom-left (blue) is now top-left, top-left (red) is top-right.
    corners = quadrant_colors(loaded.image)
    assert close_color(corners["tl"], QUADRANT_COLORS["bl"])
    assert close_color(corners["tr"], QUADRANT_COLORS["tl"])
    assert close_color(corners["br"], QUADRANT_COLORS["tr"])
    assert close_color(corners["bl"], QUADRANT_COLORS["br"])
    exif = Image.Exif()
    exif.load(loaded.exif)
    assert exif[0x0112] == 1


def test_heic_orientation_6_is_not_rotated_twice():
    """pillow-heif stores EXIF 6 as irot and applies it on decode; we must not rotate again."""
    heic = make_heic_bytes((300, 200), orientation=6)
    loaded = io.load(heic)
    assert loaded.format == "heic"
    assert loaded.orientation == 6
    assert loaded.image.size == (200, 300)
    # Same visual orientation a viewer shows for the equivalent JPEG.
    jpeg = io.load(make_jpeg_bytes((300, 200), orientation=6))
    _assert_quadrants(loaded.image, jpeg.image, tol=20)
    exif = Image.Exif()
    exif.load(loaded.exif)
    assert exif.get(0x0112, 1) == 1


def test_heic_without_orientation_and_bit_depth():
    loaded = io.load(make_heic_bytes((300, 200)))
    assert loaded.image.size == (300, 200)
    assert loaded.orientation == 1
    assert loaded.bit_depth == 8
    assert loaded.image.mode == "RGB"
    _assert_quadrants(loaded.image, make_image((300, 200), kind="quadrants"), tol=20)


@pytest.mark.parametrize("fmt", ["PNG", "WEBP", "AVIF", "TIFF"])
def test_other_formats_apply_orientation_once(fmt):
    src = make_image((300, 200), kind="quadrants")
    data = encode(src, fmt, exif=make_exif_bytes(orientation=6), **({"lossless": True} if fmt == "WEBP" else {}))
    loaded = io.load(data)
    assert loaded.image.size == (200, 300)
    assert loaded.orientation == 6
    expected = src.transpose(Image.Transpose.ROTATE_270)
    _assert_quadrants(loaded.image, expected, tol=24)


def test_loaded_image_is_detached_from_file_metadata():
    loaded = io.load(make_jpeg_bytes(gps=True, camera=True, dpi=300))
    assert loaded.image.info == {}
    assert type(loaded.image) is Image.Image


# ------------------------------------------------------------------ normalisation
def test_palette_with_transparency_becomes_rgba():
    pal = Image.new("P", (40, 30), 0)
    pal.putpalette([255, 0, 0, 0, 255, 0] + [0] * 762)
    pal.paste(1, (20, 0, 40, 30))
    data = encode(pal, "PNG", transparency=0)
    loaded = io.load(data)
    assert loaded.image.mode == "RGBA"
    assert loaded.image.getpixel((5, 5))[3] == 0
    assert loaded.image.getpixel((30, 5)) == (0, 255, 0, 255)


def test_palette_without_transparency_becomes_rgb():
    loaded = io.load(encode(make_image((40, 30), "P"), "PNG"))
    assert loaded.image.mode == "RGB"


def test_gif_first_frame_and_frame_count():
    frames = [Image.new("RGB", (20, 10), c) for c in ("red", "green", "blue")]
    buf = stdio.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=100)
    loaded = io.load(buf.getvalue())
    assert loaded.format == "gif"
    assert loaded.frames == 3
    assert loaded.image.mode == "RGB"
    assert close_color(loaded.image.getpixel((5, 5)), (255, 0, 0), 4)


def test_multipage_tiff_first_page():
    pages = [Image.new("RGB", (20, 10), c) for c in ("blue", "red")]
    buf = stdio.BytesIO()
    pages[0].save(buf, "TIFF", save_all=True, append_images=pages[1:])
    loaded = io.load(buf.getvalue())
    assert loaded.frames == 2
    assert loaded.image.getpixel((1, 1)) == (0, 0, 255)


def test_cmyk_jpeg_becomes_rgb():
    cmyk = make_image((40, 30), "CMYK")
    loaded = io.load(encode(cmyk, "JPEG", quality=95))
    assert loaded.image.mode == "RGB"
    assert loaded.icc_profile is None


def test_16bit_grayscale_png_scaled_to_8bit():
    arr = np.zeros((10, 20), dtype=np.uint16)
    arr[:, :10] = 65535
    arr[:, 10:] = 32896  # 128 * 257
    data = encode(Image.fromarray(arr), "PNG")
    loaded = io.load(data)
    assert loaded.image.mode == "L"
    assert loaded.bit_depth == 16
    assert loaded.image.getpixel((2, 2)) == 255
    assert loaded.image.getpixel((15, 2)) == 128


def test_float_tiff_scaled_to_8bit():
    arr = np.linspace(0, 1, 200, dtype=np.float32).reshape(10, 20)
    loaded = io.load(encode(Image.fromarray(arr), "TIFF"))
    assert loaded.image.mode == "L"
    assert loaded.image.getpixel((0, 0)) == 0
    assert loaded.image.getpixel((19, 9)) == 255


def test_one_bit_becomes_l_and_la_kept():
    assert io.load(encode(make_image((16, 16), "1"), "PNG")).image.mode == "L"
    assert io.load(encode(make_image((16, 16), "LA"), "PNG")).image.mode == "LA"
    assert io.load(encode(make_image((16, 16), "RGBA"), "PNG")).image.mode == "RGBA"


def test_16bit_heic_decodes_to_8bit():
    import pillow_heif

    arr = (np.random.default_rng(0).integers(0, 65535, (40, 60, 3))).astype(np.uint16)
    heif = pillow_heif.from_bytes(mode="RGB;16", size=(60, 40), data=arr.tobytes())
    buf = stdio.BytesIO()
    heif.save(buf, quality=90)
    loaded = io.load(buf.getvalue())
    assert loaded.image.mode == "RGB"
    assert loaded.bit_depth in (10, 12, 16)


def test_ico_and_jp2_inputs():
    img = make_image((64, 64), "RGBA")
    ico = io.load(encode(img, "ICO", sizes=[(64, 64), (32, 32)]))
    assert ico.format == "ico" and ico.image.size == (64, 64)
    jp2 = io.load(encode(make_image((40, 30)), "JPEG2000"))
    assert jp2.format == "jp2" and jp2.image.size == (40, 30)


# ------------------------------------------------------------------ sources & errors
def test_load_from_path_and_file_object(tmp_path):
    data = make_jpeg_bytes()
    path = tmp_path / "x.jpg"
    path.write_bytes(data)
    assert io.load(str(path)).image.size == (300, 200)
    with open(path, "rb") as fh:
        assert io.load(fh).image.size == (300, 200)


@pytest.mark.parametrize("data", [b"", b"not an image at all", b"\x89PNG\r\n\x1a\n" + b"\0" * 40])
def test_garbage_is_415(data):
    with pytest.raises(io.ImageError) as err:
        io.load(data)
    assert err.value.status == 415


def test_truncated_jpeg_is_415():
    data = make_jpeg_bytes((400, 300), kind="noise")
    with pytest.raises(io.ImageError) as err:
        io.load(data[: len(data) // 2])
    assert err.value.status == 415


def test_unsupported_pillow_format_is_415():
    with pytest.raises(io.ImageError) as err:
        io.load(encode(make_image((10, 10)), "PPM"))
    assert err.value.status == 415


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_pixel_limit_is_413(pixel_limit):
    pixel_limit(5000)
    data = encode(make_image((100, 60)), "PNG")  # 6000 px: above the limit, below Pillow's 2× error
    with pytest.raises(io.ImageError) as err:
        io.load(data)
    assert err.value.status == 413
    huge = encode(make_image((200, 200)), "PNG")  # > 2× limit → Pillow's DecompressionBombError path
    with pytest.raises(io.ImageError) as err:
        io.load(huge)
    assert err.value.status == 413
    assert io.load(encode(make_image((50, 50)), "PNG")).image.size == (50, 50)


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_pixel_limit_applies_to_heic(pixel_limit):
    data = make_heic_bytes((120, 100))
    pixel_limit(10_000)
    with pytest.raises(io.ImageError) as err:
        io.load(data)
    assert err.value.status == 413


# ------------------------------------------------------------------ dpi
@pytest.mark.parametrize("fmt,params", [("JPEG", {"dpi": (300, 300)}), ("PNG", {"dpi": (300, 300)}),
                                        ("TIFF", {"dpi": (300, 300)})])
def test_dpi_is_read(fmt, params):
    loaded = io.load(encode(make_image((30, 20)), fmt, **params))
    assert loaded.dpi == (300.0, 300.0)


def test_dpi_absent_is_none_and_exif_fallback():
    assert io.load(encode(make_image((30, 20)), "PNG")).dpi is None
    webp = encode(make_image((30, 20)), "WEBP", exif=make_exif_bytes(dpi=240))
    assert io.load(webp).dpi == (240.0, 240.0)


# ------------------------------------------------------------------ save: every format
_MODES = ["RGB", "RGBA", "L", "LA"]


@pytest.mark.parametrize("fmt", [f for f in io.OUTPUT_FORMATS if f != "pdf"])
@pytest.mark.parametrize("mode", _MODES)
def test_every_output_format_round_trips(fmt, mode):
    img = make_image((64, 48), mode)
    data = io.save(img, fmt, quality=90)
    loaded = io.load(data)
    assert loaded.format == fmt
    assert loaded.image.size == (64, 48)
    alpha_formats = {"png", "webp", "avif", "heic", "tiff", "gif"}
    if mode in ("RGBA", "LA") and fmt in alpha_formats:
        assert io.has_alpha(loaded.image)
    if fmt in ("jpeg", "bmp"):
        assert loaded.image.mode in ("RGB", "L")


@pytest.mark.parametrize("mode", _MODES)
def test_pdf_output(mode):
    data = io.save(make_image((300, 150), mode), "pdf", dpi=150)
    assert data.startswith(b"%PDF")
    boxes = re.findall(rb"/MediaBox \[ ?0 0 ([\d.]+) ([\d.]+) ?\]", data)
    assert [(float(w), float(h)) for w, h in boxes] == [(144.0, 72.0)]


def test_pdf_default_resolution_is_300():
    data = io.save(make_image((600, 300)), "pdf")
    w, h = re.findall(rb"/MediaBox \[ ?0 0 ([\d.]+) ([\d.]+) ?\]", data)[0]
    assert (float(w), float(h)) == (144.0, 72.0)


def test_save_pdf_multi_page():
    pages = [make_image((300, 300)), make_image((600, 300), "RGBA"), make_image((150, 300), "L")]
    data = io.save_pdf(pages, dpi=300)
    assert data.startswith(b"%PDF")
    boxes = [(float(w), float(h)) for w, h in re.findall(rb"/MediaBox \[ ?0 0 ([\d.]+) ([\d.]+) ?\]", data)]
    assert boxes == [(72.0, 72.0), (144.0, 72.0), (36.0, 72.0)]
    with pytest.raises(ValueError):
        io.save_pdf([])


@pytest.mark.parametrize("fmt", ["jpeg", "bmp"])
def test_alpha_flattened_onto_background(fmt):
    img = Image.new("RGBA", (20, 20), (255, 0, 0, 0))
    img.paste((0, 0, 255, 255), (10, 0, 20, 20))
    loaded = io.load(io.save(img, fmt, background="#00ff00", quality=95))
    assert loaded.image.mode == "RGB"
    assert close_color(loaded.image.getpixel((2, 10)), (0, 255, 0), 6)
    assert close_color(loaded.image.getpixel((17, 10)), (0, 0, 255), 6)


def test_rgba_to_jpeg_never_has_alpha():
    data = io.save(make_image((40, 30), "RGBA"), "jpeg")
    with Image.open(stdio.BytesIO(data)) as im:
        assert im.mode == "RGB"


def test_gif_keeps_one_bit_transparency():
    img = Image.new("RGBA", (20, 20), (255, 0, 0, 255))
    img.paste((0, 0, 0, 0), (0, 0, 10, 20))
    loaded = io.load(io.save(img, "gif"))
    assert loaded.image.mode == "RGBA"
    assert loaded.image.getpixel((2, 2))[3] == 0
    assert loaded.image.getpixel((15, 2)) == (255, 0, 0, 255)


def test_jpeg_options_quality_chroma_progressive():
    img = make_image((64, 64), kind="photo")

    def sampling(data: bytes) -> int:
        with Image.open(stdio.BytesIO(data)) as im:
            return JpegImagePlugin.get_sampling(im)

    assert sampling(io.save(img, "jpeg", quality=95)) == 0      # auto → 4:4:4 at q ≥ 90
    assert sampling(io.save(img, "jpeg", quality=80)) == 2      # auto → 4:2:0
    assert sampling(io.save(img, "jpeg", quality=80, chroma="422")) == 1
    assert sampling(io.save(img, "jpeg", quality=95, chroma="420")) == 2
    with Image.open(stdio.BytesIO(io.save(img, "jpeg", progressive=True))) as im:
        assert im.info.get("progressive")
    with Image.open(stdio.BytesIO(io.save(img, "jpeg", progressive=False))) as im:
        assert not im.info.get("progressive")
    assert len(io.save(img, "jpeg", quality=30)) < len(io.save(img, "jpeg", quality=95))


@pytest.mark.parametrize("fmt,max_err", [("webp", 0), ("heic", 0), ("avif", 4), ("png", 0), ("tiff", 0)])
def test_lossless_outputs(fmt, max_err):
    img = make_image((48, 32), kind="noise")
    loaded = io.load(io.save(img, fmt, lossless=True))
    diff = np.abs(np.asarray(loaded.image.convert("RGB"), int) - np.asarray(img, int)).max()
    assert diff <= max_err


@pytest.mark.parametrize("fmt", ["webp", "avif", "heic"])
def test_lossy_quality_changes_size(fmt):
    img = make_image((128, 96), kind="photo")
    assert len(io.save(img, fmt, quality=20)) < len(io.save(img, fmt, quality=90))


def test_large_heic_is_tiled_and_decodes(monkeypatch):
    monkeypatch.setattr(io, "_HEIC_GRID_ABOVE", 1_000_000)  # exercise the grid path on a small image
    img = make_image((1300, 900), kind="gradient")
    loaded = io.load(io.save(img, "heic", quality=60))
    assert loaded.image.size == (1300, 900)
    _assert_quadrants(loaded.image, img, tol=12)


@pytest.mark.parametrize("kwargs", [{"quality": 0}, {"quality": 101}, {"chroma": "411"}, {"colorspace": "p3"},
                                    {"dpi": 0}, {"background": "notacolour"}])
def test_save_rejects_bad_options(kwargs):
    with pytest.raises(ValueError):
        io.save(make_image(), "jpeg", **kwargs)


@pytest.mark.parametrize("fmt", ["ico", "jp2", "original", "xyz"])
def test_save_rejects_non_output_formats(fmt):
    with pytest.raises(ValueError):
        io.save(make_image(), fmt)


def test_webp_side_limit_is_a_clear_error():
    with pytest.raises(ValueError, match="16383"):
        io.save(Image.new("RGB", (16400, 2)), "webp")


# ------------------------------------------------------------------ dpi & metadata on output
@pytest.mark.parametrize("fmt", ["jpeg", "png", "tiff", "bmp", "heic", "webp", "avif"])
def test_dpi_written(fmt):
    data = io.save(make_image((40, 30)), fmt, dpi=300)
    assert io.load(data).dpi == (300.0, 300.0)


def test_dpi_tuple_and_float():
    assert io.load(io.save(make_image((40, 30)), "png", dpi=(72.0, 144.0))).dpi == (72.0, 144.0)
    assert io.load(io.save(make_image((40, 30)), "tiff", dpi=(299.5, 299.5))).dpi == (299.5, 299.5)


def test_kept_exif_gets_new_dpi_and_orientation_1():
    loaded = io.load(make_jpeg_bytes(orientation=6, camera=True))
    data = io.save(loaded.image, "jpeg", exif=loaded.exif, strip_metadata=False, dpi=600)
    exif = _exif_of(data)
    assert exif[0x010F] == "Apple"
    assert exif[0x0112] == 1
    assert float(exif[0x011A]) == 600.0
    assert io.load(data).image.size == loaded.image.size  # not rotated again on reload


@pytest.mark.parametrize("fmt", ["jpeg", "png", "webp", "avif", "heic", "tiff"])
def test_metadata_strip_and_gps_strip(fmt):
    loaded = io.load(make_jpeg_bytes(gps=True, camera=True))
    stripped = io.load(io.save(loaded.image, fmt, exif=loaded.exif))
    assert stripped.exif is None or 0x010F not in _exif_of_bytes(stripped.exif)
    no_gps = io.load(io.save(loaded.image, fmt, exif=loaded.exif, strip_metadata=False))
    parsed = _exif_of_bytes(no_gps.exif)
    assert parsed.get(0x010F) == "Apple"
    assert 0x8825 not in parsed and not parsed.get_ifd(0x8825)
    assert parsed.get_ifd(0x8769).get(0x8827) == 80
    with_gps = io.load(io.save(loaded.image, fmt, exif=loaded.exif, strip_metadata=False, strip_gps=False))
    assert _exif_of_bytes(with_gps.exif).get_ifd(0x8825).get(1) == "S"


def _exif_of_bytes(data: bytes | None) -> Image.Exif:
    exif = Image.Exif()
    if data:
        exif.load(data)
    return exif


def test_stripped_jpeg_and_png_have_no_exif_at_all():
    loaded = io.load(make_jpeg_bytes(gps=True, camera=True))
    for fmt in ("jpeg", "png"):
        with Image.open(stdio.BytesIO(io.save(loaded.image, fmt, exif=loaded.exif))) as im:
            assert "exif" not in im.info
            assert len(im.getexif()) == 0


def test_oversized_makernote_is_dropped_for_jpeg():
    exif = Image.Exif()
    exif.load(make_exif_bytes(camera=True))
    exif.get_ifd(0x8769)[0x927C] = b"\x01" * 70_000
    data = io.save(make_image(), "jpeg", exif=exif.tobytes(), strip_metadata=False)
    parsed = _exif_of(data)
    assert parsed[0x010F] == "Apple"
    assert 0x927C not in parsed.get_ifd(0x8769)


def test_concurrent_saves_of_shared_image_do_not_leak_metadata():
    """Pillow keeps encoder settings on the Image object; shared (cached) images must stay isolated."""
    loaded = io.load(make_jpeg_bytes((200, 150), gps=True, camera=True))
    img, errors = loaded.image, []

    def keep_meta():
        for _ in range(15):
            io.save(img, "png", exif=loaded.exif, strip_metadata=False, strip_gps=False)

    def strip_meta():
        for _ in range(15):
            data = io.save(img, "png", exif=loaded.exif)
            with Image.open(stdio.BytesIO(data)) as im:
                if "exif" in im.info:
                    errors.append("leak")

    threads = [threading.Thread(target=t) for t in (keep_meta, strip_meta, keep_meta, strip_meta)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert img.info == {}


# ------------------------------------------------------------------ colour management
def _embedded_icc(data: bytes) -> bytes | None:
    with Image.open(stdio.BytesIO(data)) as im:
        return im.info.get("icc_profile")


def test_builtin_srgb_profile_is_valid_and_neutral():
    prof = ImageCms.ImageCmsProfile(stdio.BytesIO(io.SRGB_ICC))
    assert "sRGB" in ImageCms.getProfileDescription(prof)
    assert prof.profile.version < 3  # v2 for the widest compatibility
    grid = np.stack(np.meshgrid(*[np.arange(0, 256, 15)] * 3, indexing="ij"), -1).reshape(-1, 1, 3).astype(np.uint8)
    img = Image.fromarray(grid)
    out = ImageCms.profileToProfile(img, prof, ImageCms.createProfile("sRGB"))
    assert np.abs(np.asarray(out, int) - np.asarray(img, int)).max() <= 1


def test_p3_converted_to_srgb_and_srgb_embedded(p3_profile):
    img = Image.new("RGB", (16, 16), (40, 200, 60))
    out = io.to_srgb(img, p3_profile)
    r, g, b = out.getpixel((0, 0))
    assert g >= 200 and r < 40  # P3 green is more saturated than sRGB green can express
    data = io.save(img, "jpeg", icc_profile=p3_profile, quality=95)
    icc = _embedded_icc(data)
    assert icc == io.SRGB_ICC
    assert close_color(io.load(data).image.getpixel((8, 8)), (r, g, b), 4)
    gray = io.to_srgb(Image.new("RGB", (4, 4), (128, 128, 128)), p3_profile).getpixel((0, 0))
    assert max(gray) - min(gray) <= 2


@pytest.mark.parametrize("fmt", ["png", "webp", "avif", "heic", "tiff"])
def test_srgb_profile_embedded_for_formats_with_icc(fmt, p3_profile):
    data = io.save(make_image((16, 16)), fmt, icc_profile=p3_profile)
    assert io.icc_description(io.load(data).icc_profile).startswith("sRGB")


def test_colorspace_keep_embeds_original_profile(p3_profile):
    img = Image.new("RGB", (16, 16), (40, 200, 60))
    data = io.save(img, "png", icc_profile=p3_profile, colorspace="keep")
    loaded = io.load(data)
    assert loaded.icc_profile == p3_profile
    assert loaded.image.getpixel((0, 0)) == (40, 200, 60)
    assert io.icc_description(loaded.icc_profile) == "Display P3 (test)"


def test_icc_load_and_preview_convert(p3_profile):
    data = encode(Image.new("RGB", (32, 32), (40, 200, 60)), "PNG", icc_profile=p3_profile)
    loaded = io.load(data)
    assert loaded.icc_profile == p3_profile
    preview, mime = io.preview(loaded.image, loaded.icc_profile)
    assert mime == "image/jpeg"
    px = Image.open(stdio.BytesIO(preview)).getpixel((16, 16))
    assert px[0] < 30


def test_to_srgb_no_ops_and_alpha(p3_profile):
    img = make_image((8, 8), "RGBA")
    assert io.to_srgb(img, None) is img
    assert io.to_srgb(img, b"garbage") is img
    assert io.to_srgb(img, io.SRGB_ICC) is img
    gray = make_image((8, 8), "L")
    assert io.to_srgb(gray, p3_profile) is gray
    converted = io.to_srgb(img, p3_profile)
    assert converted.mode == "RGBA"
    assert np.array_equal(np.asarray(converted)[..., 3], np.asarray(img)[..., 3])


def test_gray_image_never_gets_rgb_profile(p3_profile):
    data = io.save(make_image((16, 16), "L"), "jpeg", icc_profile=p3_profile)
    assert _embedded_icc(data) is None
    data = io.save(make_image((16, 16), "L"), "jpeg", icc_profile=p3_profile, colorspace="keep")
    assert _embedded_icc(data) is None


def test_garbage_icc_profile_does_not_crash():
    data = encode(make_image((16, 16)), "PNG", icc_profile=b"definitely not icc")
    loaded = io.load(data)
    assert io.icc_description(loaded.icc_profile) is None
    io.save(loaded.image, "jpeg", icc_profile=loaded.icc_profile)
    io.preview(loaded.image, loaded.icc_profile)


def test_cmyk_with_mismatched_profile_falls_back_to_plain_conversion():
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    loaded = io.load(encode(Image.new("CMYK", (20, 20), (0, 255, 255, 0)), "JPEG", icc_profile=srgb, quality=95))
    assert loaded.image.mode == "RGB"
    assert loaded.icc_profile is None
    assert close_color(loaded.image.getpixel((10, 10)), (255, 0, 0), 8)


# ------------------------------------------------------------------ flatten & preview
def test_flatten():
    img = Image.new("RGBA", (4, 4), (255, 0, 0, 128))
    out = io.flatten(img, "#0000ff")
    assert out.mode == "RGB"
    assert close_color(out.getpixel((0, 0)), (128, 0, 127), 2)
    la = Image.new("LA", (4, 4), (0, 0))
    assert io.flatten(la, "white").mode == "L"
    assert io.flatten(la, "red").getpixel((0, 0)) == (255, 0, 0)
    rgb = make_image((4, 4))
    assert io.flatten(rgb, "#000") is rgb
    assert io.flatten(img, "transparent").getpixel((0, 0)) == (255, 127, 127)


def test_preview_downscales_and_picks_mime():
    data, mime = io.preview(make_image((3000, 1000)), max_side=600)
    assert mime == "image/jpeg"
    assert Image.open(stdio.BytesIO(data)).size == (600, 200)
    data, mime = io.preview(make_image((100, 50), "RGBA"), max_side=1600)
    assert mime == "image/png"
    assert Image.open(stdio.BytesIO(data)).size == (100, 50)
    opaque = make_image((40, 30), "RGB").convert("RGBA")
    assert io.preview(opaque)[1] == "image/jpeg"
    assert io.preview(make_image((40, 30), "L"))[1] == "image/jpeg"
    with pytest.raises(ValueError):
        io.preview(make_image(), max_side=0)


# ------------------------------------------------------------------ TIFF + kept EXIF never reaches libtiff unfiltered
_TIFF_EXIF_SCRIPT = """
import sys
from io import BytesIO
from PIL import Image
from app.imaging import io as hio
tag, value, camera = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3] == "1"
ex = Image.Exif()
ex[0x010F] = "Scanner"
ex[tag] = value
if camera:
    ex.get_ifd(0x8769)[0x8827] = 100
buf = BytesIO()
Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, "JPEG", exif=ex.tobytes())
loaded = hio.load(buf.getvalue())
data = hio.save(loaded.image, "tiff", exif=loaded.exif, strip_metadata=False)
with Image.open(BytesIO(data)) as out:
    print(out.getexif().get(0x010F), out.info.get("compression"), out.getpixel((4, 4)))
"""


@pytest.mark.parametrize("tag", [0x0194, 0xC612, 0xC614, 0xC621])  # TIFF-FX VersionYear, DNG tags
@pytest.mark.parametrize("camera", [False, True])
def test_tiff_with_odd_ifd0_exif_tag_does_not_crash_the_process(tag, camera):
    """libtiff segfaults on IFD0 tags whose value doesn't match its field definition; a segfault can't
    be caught, so the save runs in a child process."""
    import subprocess
    import sys
    from pathlib import Path

    proc = subprocess.run([sys.executable, "-c", _TIFF_EXIF_SCRIPT, str(tag), "6", "1" if camera else "0"],
                          cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    make, compression, _pixel = proc.stdout.split(" ", 2)
    assert make == "Scanner"
    assert compression == ("raw" if camera else "tiff_lzw")  # LZW kept whenever libtiff can write the EXIF


def test_libtiff_exif_keeps_only_well_typed_ifd0_tags():
    ex = Image.Exif()
    ex[0x010F], ex[0x0131], ex[0x0194], ex[0xC612], ex[0x011A], ex[0x0128] = "Acme", "Editor 2", 6, 1, 300.0, 2
    kept = _exif_of_bytes(io._libtiff_exif(ex.tobytes()))
    assert dict(kept) == {0x010F: "Acme", 0x0131: "Editor 2", 0x011A: 300.0, 0x0128: 2}
    data = io.save(make_image((16, 16)), "tiff", exif=ex.tobytes(), strip_metadata=False, dpi=150)
    loaded = io.load(data)
    assert loaded.dpi == (150.0, 150.0)
    assert _exif_of_bytes(loaded.exif).get(0x010F) == "Acme"


def test_gps_only_exif_with_strip_gps_takes_the_filtered_libtiff_path():
    ex = Image.Exif()
    ex[0x010F], ex[0x0194] = "Phone", 6
    ex.get_ifd(0x8825)[1] = "N"
    data = io.save(make_image((16, 16)), "tiff", exif=ex.tobytes(), strip_metadata=False)
    with Image.open(stdio.BytesIO(data)) as im:
        assert im.info["compression"] == "tiff_lzw"
        assert im.getexif().get(0x010F) == "Phone" and 0x0194 not in im.getexif()


# ------------------------------------------------------------------ nclx (CICP) colour in HEIF/AVIF
_P3_PATCH, _P3_IN_SRGB = (200, 100, 50), (215, 93, 31)  # exact P3 (sRGB curve) → sRGB, computed analytically


def _srgb_eotf(v: np.ndarray) -> np.ndarray:
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)


def _srgb_oetf(v: np.ndarray) -> np.ndarray:
    v = np.clip(v, 0.0, 1.0)
    return np.where(v <= 0.0031308, v * 12.92, 1.055 * v ** (1 / 2.4) - 0.055)


def _rgb_to_xyz(prims, white) -> np.ndarray:
    def xyz(x, y):
        return np.array([x / y, 1.0, (1 - x - y) / y])
    m = np.stack([xyz(*p) for p in prims], axis=1)
    return m * np.linalg.solve(m, xyz(*white))


def _reference_to_srgb(rgb8: np.ndarray, prims, white) -> np.ndarray:
    """Linear-light matrix conversion (Bradford white adaptation) — independent of ICC/LittleCMS."""
    d65 = (0.3127, 0.3290)
    brad = np.array([[0.8951, 0.2664, -0.1614], [-0.7502, 1.7135, 0.0367], [0.0389, -0.0685, 1.0296]])

    def xyz(x, y):
        return np.array([x / y, 1.0, (1 - x - y) / y])
    adapt = np.linalg.inv(brad) @ np.diag((brad @ xyz(*d65)) / (brad @ xyz(*white))) @ brad
    srgb = _rgb_to_xyz([(0.64, 0.33), (0.30, 0.60), (0.15, 0.06)], d65)
    m = np.linalg.inv(srgb) @ adapt @ _rgb_to_xyz(prims, white)
    lin = _srgb_eotf(rgb8 / 255.0) @ m.T
    return np.round(_srgb_oetf(lin) * 255)


@pytest.mark.parametrize("code", [12, 11, 9])  # Display P3, DCI-P3, BT.2020
def test_nclx_profiles_match_linear_light_reference(code):
    name, red, green, blue, white = io._NCLX_PRIMARIES[code]
    rgb = np.random.default_rng(code).integers(0, 256, (64, 64, 3)).astype(np.uint8)
    icc = io._nclx_icc(code, 13)
    assert name in io.icc_description(icc)
    out = np.asarray(io.to_srgb(Image.fromarray(rgb), icc), dtype=float)
    ref = _reference_to_srgb(rgb.reshape(-1, 3).astype(float), [red, green, blue], white).reshape(rgb.shape)
    err = np.abs(out - ref)
    assert err.max() <= 1 and err.mean() < 0.05


def test_nclx_profile_only_for_wide_gamut_with_known_curves():
    assert io._nclx_icc(1, 13) is None      # sRGB
    assert io._nclx_icc(2, 2) is None       # unspecified
    assert io._nclx_icc(12, 16) is None     # PQ needs tone mapping, not a matrix
    assert io._nclx_icc(12, 18) is None     # HLG
    assert io._nclx_icc(99, 13) is None
    assert io._nclx_icc(12, 1) == io._nclx_icc(12, 13)  # BT.709 curve is rendered like sRGB
    assert "linear" in io.icc_description(io._nclx_icc(1, 8))


def _nclx_heic(primaries: int) -> bytes:
    buf = stdio.BytesIO()
    Image.new("RGB", (64, 64), _P3_PATCH).save(buf, "HEIF", quality=-1, chroma="444", color_primaries=primaries,
                                               transfer_characteristics=13, matrix_coefficients=0, full_range_flag=1)
    return buf.getvalue()


def test_nclx_p3_heic_is_converted_to_srgb():
    loaded = io.load(_nclx_heic(12))
    assert loaded.image.getpixel((8, 8)) == _P3_PATCH  # pixels stay as decoded; the profile describes them
    assert "P3" in io.icc_description(loaded.icc_profile)
    from app.imaging import info
    assert "P3" in info.describe(loaded, name="p3.heic", size_bytes=1)["icc"]
    out = io.load(io.save(loaded.image, "png", icc_profile=loaded.icc_profile))
    assert close_color(out.image.getpixel((8, 8)), _P3_IN_SRGB, 2)
    assert io.icc_description(out.icc_profile).startswith("sRGB")
    preview, _ = io.preview(loaded.image, loaded.icc_profile)
    assert close_color(Image.open(stdio.BytesIO(preview)).getpixel((8, 8)), _P3_IN_SRGB, 3)
    kept = io.load(io.save(loaded.image, "png", icc_profile=loaded.icc_profile, colorspace="keep"))
    assert kept.image.getpixel((8, 8)) == _P3_PATCH and kept.icc_profile == loaded.icc_profile


def test_nclx_srgb_heic_is_left_alone():
    loaded = io.load(_nclx_heic(1))
    assert loaded.icc_profile is None
    out = io.load(io.save(loaded.image, "png", icc_profile=loaded.icc_profile))
    assert out.image.getpixel((8, 8)) == _P3_PATCH


def _avif_with_nclx(primaries: int, transfer: int) -> bytes:
    """Pillow's AVIF writer has no CICP options: patch the nclx 'colr' box it writes."""
    data = bytearray(encode(Image.new("RGB", (64, 64), _P3_PATCH), "AVIF", quality=100, subsampling="4:4:4"))
    pos = data.find(b"colrnclx")
    assert pos > 0
    data[pos + 8:pos + 12] = primaries.to_bytes(2, "big") + transfer.to_bytes(2, "big")
    return bytes(data)


def test_nclx_p3_avif_is_converted_to_srgb():
    plain = io.load(_avif_with_nclx(1, 13))
    assert plain.icc_profile is None
    loaded = io.load(_avif_with_nclx(12, 13))
    assert "P3" in io.icc_description(loaded.icc_profile)
    expected = io.to_srgb(Image.new("RGB", (1, 1), plain.image.getpixel((8, 8))), loaded.icc_profile).getpixel((0, 0))
    out = io.load(io.save(loaded.image, "png", icc_profile=loaded.icc_profile)).image.getpixel((8, 8))
    assert out == expected and close_color(out, _P3_IN_SRGB, 4)
    assert io.load(_avif_with_nclx(12, 16)).icc_profile is None  # PQ: left alone


def test_avif_box_parser_survives_garbage():
    assert io._avif_nclx(b"") is None
    assert io._avif_nclx(b"\x00\x00\x00\x10ftypavif\x00\x00\x00\x00") is None
    assert io._avif_nclx(b"\x00\x00\x00\x01meta" + b"\xff" * 8) is None
    data = _avif_with_nclx(12, 13)
    for cut in range(0, len(data), 7):
        io._avif_nclx(data[:cut])  # truncated headers never raise


# ------------------------------------------------------------------ gray ICC profiles
@pytest.mark.parametrize("gamma,expected", [(1.8, 147), (2.2, 128), (1.0, 188)])
def test_gray_profile_converted_to_srgb_gray(gamma, expected):
    from tests.conftest import make_gray_icc_profile

    icc = make_gray_icc_profile(gamma, f"Gray Gamma {gamma}")
    loaded = io.load(encode(Image.new("L", (32, 32), 128), "JPEG", quality=100, icc_profile=icc))
    assert loaded.icc_profile == icc and loaded.image.mode == "L"
    out = io.load(io.save(loaded.image, "jpeg", icc_profile=loaded.icc_profile))
    assert out.image.mode == "L" and abs(out.image.getpixel((8, 8)) - expected) <= 2
    preview, _ = io.preview(loaded.image, loaded.icc_profile)
    assert abs(Image.open(stdio.BytesIO(preview)).getpixel((8, 8)) - expected) <= 2
    kept = io.load(io.save(loaded.image, "png", icc_profile=icc, colorspace="keep"))
    assert kept.image.getpixel((8, 8)) == 128 and kept.icc_profile == icc


def test_gray_profile_keeps_alpha():
    from tests.conftest import make_gray_icc_profile

    la = Image.new("LA", (8, 8), (128, 100))
    out = io.to_srgb(la, make_gray_icc_profile(1.8))
    assert out.mode == "LA" and abs(out.getpixel((0, 0))[0] - 147) <= 1 and out.getpixel((0, 0))[1] == 100
    assert io.to_srgb(la, io.SRGB_ICC) is la


# ------------------------------------------------------------------ HEIC quality scale
def test_heic_quality_mapping_keeps_end_points_and_order():
    mapped = [io._heic_quality(q) for q in range(1, 101)]
    assert mapped[0] == 1 and io._heic_quality(90) == 70 and mapped[-1] == 100
    assert all(a <= b for a, b in zip(mapped, mapped[1:]))


def test_heic_q90_is_not_larger_than_jpeg_q90():
    from pathlib import Path

    img = Image.open(Path(__file__).parent / "fixtures" / "astronaut.png").convert("RGB")
    heic, jpeg = io.save(img, "heic", quality=90), io.save(img, "jpeg", quality=90)
    assert len(heic) <= len(jpeg)
    diff = np.abs(np.asarray(io.load(heic).image, float) - np.asarray(img, float))
    assert 10 * np.log10(255 ** 2 / (diff ** 2).mean()) > 35  # still high quality
    assert len(io.save(img, "heic", quality=100)) > len(heic)


# ------------------------------------------------------------------ palette steps keep alpha
@pytest.mark.parametrize("colors", [256, 64, 16])
def test_png_palette_keeps_clear_and_opaque_alpha_exact(colors):
    from tests.conftest import make_cutout

    cut = make_cutout()
    src = np.asarray(cut.getchannel("A"))
    data = io._encode(io._prepare(cut, "png"), colors=colors, optimize=True)
    with Image.open(stdio.BytesIO(data)) as im:
        assert im.mode == "P" and len(im.getcolors(256)) <= colors
        alpha = np.asarray(im.convert("RGBA").getchannel("A"))
    assert (alpha[src == 0] == 0).all()
    assert (alpha[src == 255] == 255).all()
    edge = (src > 0) & (src < 255)
    plain = np.asarray(cut.quantize(colors, method=Image.Quantize.FASTOCTREE).convert("RGBA").getchannel("A"))
    plain_err = np.abs(plain[edge].astype(int) - src[edge]).mean()
    assert np.abs(alpha[edge].astype(int) - src[edge]).mean() <= 1.25 * plain_err + 2  # soft edges stay as good


@pytest.mark.parametrize("colors", [256, 16])
def test_tiff_palette_step_keeps_alpha(colors):
    from tests.conftest import make_cutout

    cut = make_cutout()
    full = io._encode(io._prepare(cut, "tiff"))
    data = io._encode(io._prepare(cut, "tiff"), colors=colors)
    assert len(data) < len(full)
    loaded = io.load(data)
    assert loaded.image.mode == "RGBA"
    assert np.array_equal(np.asarray(loaded.image.getchannel("A")), np.asarray(cut.getchannel("A")))
    assert len(loaded.image.convert("RGB").getcolors(1 << 16)) <= colors
    gray = io.load(io._encode(io._prepare(cut.convert("LA"), "tiff"), colors=colors)).image
    assert gray.mode == "LA" and np.array_equal(np.asarray(gray.getchannel("A")), np.asarray(cut.getchannel("A")))


def test_opaque_rgba_palette_png_is_plain_palette():
    data = io._encode(io._prepare(make_image((64, 48)).convert("RGBA"), "png"), colors=32)
    with Image.open(stdio.BytesIO(data)) as im:
        assert im.mode == "P" and "transparency" not in im.info


# ------------------------------------------------------------------ JPEG encoder buffer
def test_noisy_jpeg_needs_no_retry_and_prints_nothing(capfd):
    """libjpeg can't suspend with optimize/progressive: the buffer must fit the whole file up front."""
    img = make_image((600, 400), kind="noise")
    for quality, chroma in ((92, "auto"), (100, "444"), (100, "420")):
        data = io.save(img, "jpeg", quality=quality, chroma=chroma, progressive=True)
        assert io.load(data).image.size == (600, 400)
    gray = io.save(make_image((600, 400), "L", kind="noise"), "jpeg", quality=100)
    assert len(gray) > 600 * 400 * 0.9
    out, err = capfd.readouterr()
    assert "Suspension" not in err + out
    from PIL import ImageFile
    assert ImageFile.MAXBLOCK == 65536  # restored after the encode
