"""app.imaging.info.describe: FileInfo fields, camera/GPS extraction and robust EXIF dumping."""
from __future__ import annotations

import json

from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from app.imaging import info, io
from tests.conftest import encode, make_heic_bytes, make_image, make_jpeg_bytes

io.register_plugins()


def _describe(data: bytes, **kwargs) -> dict:
    return info.describe(io.load(data), name="IMG_0001.JPG", size_bytes=len(data), **kwargs)


def test_jpeg_with_camera_gps_dpi():
    data = make_jpeg_bytes((300, 200), orientation=6, gps=True, dpi=300, camera=True)
    d = _describe(data)
    assert d["name"] == "IMG_0001.JPG"
    assert d["format"] == "jpeg" and d["mime"] == "image/jpeg"
    assert d["size_bytes"] == len(data)
    assert (d["width"], d["height"]) == (200, 300)
    assert d["mode"] == "RGB" and d["has_alpha"] is False
    assert d["frames"] == 1 and d["bit_depth"] == 8
    assert d["dpi"] == [300, 300]
    assert d["print_size_in"] == [0.67, 1.0]
    assert d["orientation"] == 6
    assert d["icc"] is None
    assert d["camera"] == {
        "make": "Apple", "model": "iPhone 15 Pro", "lens": "iPhone 15 Pro back triple camera 6.86mm f/1.78",
        "datetime": "2026:09:01 10:22:01", "iso": 80, "exposure": "1/120", "f_number": 1.8, "focal_length_mm": 6.8,
    }
    assert d["gps"] == {"lat": -12.97, "lon": -77.59}
    assert "exif" not in d
    json.dumps(d)


def test_full_exif_dump_is_readable_and_json_safe():
    data = make_jpeg_bytes((300, 200), orientation=3, gps=True, camera=True)
    d = _describe(data, full_exif=True)
    exif = d["exif"]
    assert exif["Make"] == "Apple"
    assert exif["Orientation"] == 3  # the file's own value
    assert exif["ExposureTime"] == round(1 / 120, 6)
    assert exif["FNumber"] == 1.8
    assert exif["ISOSpeedRatings"] == 80
    assert exif["GPSLatitudeRef"] == "S"
    assert exif["GPSLatitude"] == [12.0, 58.0, 12.0]
    assert "ExifOffset" not in exif and "GPSInfo" not in exif
    json.dumps(exif)


def test_no_metadata():
    d = _describe(encode(make_image((64, 48)), "PNG"), full_exif=True)
    assert d["dpi"] is None and d["print_size_in"] is None
    assert d["camera"] is None and d["gps"] is None
    assert d["orientation"] == 1
    assert d["exif"] == {}


def test_heic_describe():
    data = make_heic_bytes((300, 200), orientation=6, camera=True, gps=True)
    d = info.describe(io.load(data), name="IMG_0002.HEIC", size_bytes=len(data))
    assert d["format"] == "heic" and d["mime"] == "image/heic"
    assert (d["width"], d["height"]) == (200, 300)
    assert d["orientation"] == 6
    assert d["camera"]["model"] == "iPhone 15 Pro"
    assert d["gps"] == {"lat": -12.97, "lon": -77.59}


def test_alpha_icc_and_bit_depth(p3_profile):
    rgba = encode(make_image((32, 32), "RGBA"), "PNG", icc_profile=p3_profile)
    d = _describe(rgba)
    assert d["mode"] == "RGBA" and d["has_alpha"] is True
    assert d["icc"] == "Display P3 (test)"
    opaque = _describe(encode(make_image((32, 32), "RGB").convert("RGBA"), "PNG"))
    assert opaque["mode"] == "RGBA" and opaque["has_alpha"] is False


def test_animated_gif_frames():
    frames = [Image.new("RGB", (10, 10), c) for c in ("red", "blue")]
    import io as stdio

    buf = stdio.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:])
    assert _describe(buf.getvalue())["frames"] == 2


def test_odd_exif_never_raises():
    exif = Image.Exif()
    exif[0x010F] = "Weird\x00Cam  "
    exif[0x0110] = b"Model\x00"
    exif[0x011A] = IFDRational(0, 0)   # 0/0 resolution
    exif[0x0131] = "x" * 5000
    sub = exif.get_ifd(0x8769)
    sub[0x829A] = IFDRational(0, 0)
    sub[0x829D] = IFDRational(1, 0)
    sub[0x8827] = (100, 200)
    sub[0x927C] = b"\x00\x01\x02" * 1000  # MakerNote blob
    sub[0x9000] = b"0232"
    gps = exif.get_ifd(0x8825)
    gps[2] = (IFDRational(1, 0), IFDRational(2, 1))
    gps[4] = (IFDRational(0, 0), IFDRational(0, 0), IFDRational(0, 0))
    data = encode(make_image((20, 20)), "JPEG", exif=exif.tobytes())
    d = _describe(data, full_exif=True)
    json.dumps(d)
    assert d["camera"]["make"] == "WeirdCam"
    assert d["camera"]["iso"] == 100
    assert d["camera"]["exposure"] is None and d["camera"]["f_number"] is None
    assert d["gps"] is None
    assert d["dpi"] is None
    assert "MakerNote" not in d["exif"]
    assert d["exif"]["ExifVersion"] == "0232"
    assert len(d["exif"]["Software"]) <= 512


def test_corrupt_exif_bytes_in_loaded():
    loaded = io.load(encode(make_image((20, 20)), "PNG"))
    loaded.exif = b"Exif\x00\x00garbage-garbage-garbage"
    d = info.describe(loaded, name="x.png", size_bytes=1, full_exif=True)
    assert d["camera"] is None and d["exif"] == {}
