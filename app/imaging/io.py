"""Decode, normalise, colour-manage and encode images (PLAN.md §4).

``load`` returns the first frame EXIF-oriented exactly once in RGB/RGBA/L/LA; ``save`` writes
every output format with correct colour, alpha, DPI and metadata handling.
"""
from __future__ import annotations

import functools
import math
import numbers
import os
import struct
import threading
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from io import BytesIO
from typing import Any, BinaryIO

import numpy as np
from PIL import ExifTags, Image, ImageCms, ImageFile, features

from .ops import parse_color

__all__ = [
    "FEATURES", "INPUT_FORMATS", "OUTPUT_FORMATS", "ImageError", "Loaded", "register_plugins", "max_pixels",
    "load", "save", "save_pdf", "to_srgb", "flatten", "preview", "has_alpha", "mime_for", "ext_for",
    "canonical_format", "icc_description",
]

FEATURES: dict[str, bool] = {"heic": False, "avif": False}
DEFAULT_MAX_PIXELS = 50_000_000  # matches app.config (HEYP_MAX_PIXELS default); phones produce 12–48 MP
INPUT_FORMATS = ("heic", "jpeg", "png", "webp", "avif", "tiff", "bmp", "gif", "ico", "jp2")
OUTPUT_FORMATS = ("jpeg", "png", "webp", "avif", "heic", "tiff", "bmp", "gif", "pdf")

_PIL_TO_CANON = {
    "JPEG": "jpeg", "MPO": "jpeg", "PNG": "png", "WEBP": "webp", "AVIF": "avif", "HEIF": "heic",
    "TIFF": "tiff", "BMP": "bmp", "DIB": "bmp", "GIF": "gif", "ICO": "ico", "JPEG2000": "jp2",
}
_ALIASES = {
    "jpg": "jpeg", "jpe": "jpeg", "jfif": "jpeg", "pjpeg": "jpeg", "mpo": "jpeg", "heif": "heic", "hif": "heic",
    "heics": "heic", "heifs": "heic", "tif": "tiff", "dib": "bmp", "x-ms-bmp": "bmp", "icon": "ico",
    "x-icon": "ico", "vnd.microsoft.icon": "ico", "jpeg2000": "jp2", "j2k": "jp2", "jpx": "jp2", "jpf": "jp2",
    "j2c": "jp2", "x-png": "png", "apng": "png",
}
_MIME = {
    "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "avif": "image/avif", "heic": "image/heic",
    "tiff": "image/tiff", "bmp": "image/bmp", "gif": "image/gif", "pdf": "application/pdf",
    "ico": "image/vnd.microsoft.icon", "jp2": "image/jp2",
}
_EXT = {"jpeg": "jpg"}
_KNOWN = set(_MIME)

_TAG_ORIENTATION = ExifTags.Base.Orientation  # 0x0112
_TAG_XRES, _TAG_YRES, _TAG_RES_UNIT = 0x011A, 0x011B, 0x0128
_TAG_EXIF_IFD, _TAG_GPS_IFD, _TAG_MAKERNOTE = 0x8769, 0x8825, 0x927C
# TIFF structural / bulky tags that must not be carried into another file's EXIF.
_TIFF_ONLY_TAGS = frozenset({
    254, 255, 256, 257, 258, 259, 262, 263, 266, 273, 277, 278, 279, 280, 281, 284, 292, 293, 317, 320,
    322, 323, 324, 325, 330, 338, 339, 340, 341, 347, 513, 514, 700, 33723, 34377, 34675, 37724,
})
# IFD0 text tags libtiff takes as plain ASCII fields: ImageDescription, Make, Model, Software, DateTime,
# Artist, HostComputer, Copyright. libtiff receives every IFD0 tag as a field of its own and a value that
# does not match its field definition (DNG, TIFF-FX, Predictor, … tags) crashes the process natively.
_LIBTIFF_TEXT_TAGS = frozenset({270, 271, 272, 305, 306, 315, 316, 33432})
_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180, 4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE, 6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_HEAD_BYTES = 65536
_HEIC_GRID_ABOVE = 35_651_584  # HEVC level 6 max picture size: larger images must be a 512 px tile grid
_PNG_OPTIMIZE_MAX_PIXELS = 1_000_000
_WEBP_MAX_SIDE = 16383
_WEBP_FAST_ABOVE = 20_000_000
_JPEG_MAX_SIDE = 65535
_JPEG_MAX_EXIF = 65533

_lock = threading.Lock()
_registered = False
_max_pixels = DEFAULT_MAX_PIXELS
_open_formats: tuple[str, ...] = ()


class ImageError(Exception):
    """Decoding failure with the HTTP status the API should use (413 too large, 415 undecodable)."""

    def __init__(self, message: str, status: int = 415) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Loaded:
    image: Image.Image          # first frame, EXIF-oriented once, mode ∈ {RGB, RGBA, L, LA}
    format: str                 # canonical lowercase format
    exif: bytes | None          # orientation reset to 1
    icc_profile: bytes | None
    dpi: tuple[float, float] | None
    frames: int
    bit_depth: int
    orientation: int            # original EXIF orientation (1 if none)


# ------------------------------------------------------------------ setup
def _env_max_pixels() -> int:
    raw = os.environ.get("HEYP_MAX_PIXELS", "").strip().replace("_", "")
    if not raw:
        return DEFAULT_MAX_PIXELS
    try:
        value = int(float(raw))
    except ValueError:
        return DEFAULT_MAX_PIXELS
    return value if value > 0 else DEFAULT_MAX_PIXELS


def register_plugins() -> dict[str, bool]:
    """Register HEIF/AVIF support and the pixel limit; safe to call repeatedly (re-reads HEYP_MAX_PIXELS)."""
    global _registered, _max_pixels, _open_formats
    with _lock:
        if not _registered:
            try:
                import pillow_heif

                pillow_heif.register_heif_opener(thumbnails=False, depth_images=False, aux_images=False)
                FEATURES["heic"] = True
            except Exception:  # missing wheel / libheif → HEIC unsupported, everything else works
                FEATURES["heic"] = False
            FEATURES["avif"] = bool(features.check("avif"))
            Image.init()
            _open_formats = tuple(f for f in _PIL_TO_CANON if f in Image.OPEN)
            # We enforce the limit ourselves (413) right after opening; Pillow's warning adds nothing.
            warnings.filterwarnings("ignore", category=Image.DecompressionBombWarning)
            _registered = True
        _max_pixels = _env_max_pixels()
        Image.MAX_IMAGE_PIXELS = _max_pixels
    return dict(FEATURES)


def _ensure_registered() -> None:
    if not _registered:
        register_plugins()


def max_pixels() -> int:
    """Current decompression-bomb limit in pixels (HEYP_MAX_PIXELS)."""
    _ensure_registered()
    return _max_pixels


# ------------------------------------------------------------------ format names
def canonical_format(name: str) -> str:
    """Normalise a format name, extension or MIME type (``JPG``, ``.heif``, ``image/tiff``) → canonical."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"Unknown image format: {name!r}")
    key = name.strip().lower().lstrip(".")
    if "/" in key:
        key = key.split("/", 1)[1]
    key = _ALIASES.get(key, key)
    if key not in _KNOWN:
        raise ValueError(f"Unknown image format: {name!r}")
    return key


def mime_for(fmt: str) -> str:
    """MIME type for a format name."""
    return _MIME[canonical_format(fmt)]


def ext_for(fmt: str) -> str:
    """File extension without the dot (``jpeg`` → ``jpg``)."""
    key = canonical_format(fmt)
    return _EXT.get(key, key)


# ------------------------------------------------------------------ helpers
def has_alpha(img: Image.Image) -> bool:
    """True when the image has an alpha channel that is not fully opaque."""
    if img.mode in ("RGBA", "LA", "PA", "RGBa", "La"):
        return img.getextrema()[-1][0] < 255
    return img.mode == "P" and "transparency" in img.info


def _round_dpi(value: float) -> float:
    nearest = round(value)
    return float(nearest) if abs(value - nearest) < 0.05 else round(value, 2)


def _valid_dpi(pair: Any) -> tuple[float, float] | None:
    try:
        x, y = (float(v) for v in pair)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(x) and math.isfinite(y)) or not (1 <= x <= 100_000 and 1 <= y <= 100_000):
        return None
    return _round_dpi(x), _round_dpi(y)


def _dpi_from(info: dict, exif: Image.Exif | None) -> tuple[float, float] | None:
    dpi = _valid_dpi(info.get("dpi")) if info.get("dpi") is not None else None
    if dpi or exif is None:
        return dpi
    try:
        xres, yres = exif.get(_TAG_XRES), exif.get(_TAG_YRES)
        unit = exif.get(_TAG_RES_UNIT, 2)
        if xres is None:
            return None
        yres = xres if yres is None else yres
        factor = {2: 1.0, 3: 2.54}.get(int(unit))
        if factor is None:
            return None
        return _valid_dpi((float(xres) * factor, float(yres) * factor))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _valid_orientation(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 1
    return number if 1 <= number <= 8 else 1


def _safe_getexif(im: Image.Image) -> Image.Exif:
    try:
        return im.getexif()
    except Exception:  # corrupt EXIF must never break decoding
        return Image.Exif()


def _parse_exif(data: bytes | Image.Exif | None) -> Image.Exif | None:
    if data is None:
        return None
    if isinstance(data, Image.Exif):
        data = data.tobytes()
    if not data:
        return None
    exif = Image.Exif()
    try:
        exif.load(bytes(data))
    except Exception:
        return None
    return exif if len(exif) else None


def _serialize_exif(exif: Image.Exif) -> bytes | None:
    try:
        return exif.tobytes() if len(exif) else None
    except Exception:
        return None


def _clean_exif(raw: bytes | None) -> bytes | None:
    """Re-serialise EXIF with orientation 1 and without TIFF structural tags; None if empty/unreadable."""
    exif = _parse_exif(raw)
    if exif is None:
        return None
    for tag in list(exif.keys()):
        if tag in _TIFF_ONLY_TAGS:
            del exif[tag]
    if _TAG_ORIENTATION in exif:
        exif[_TAG_ORIENTATION] = 1
    return _serialize_exif(exif)


def _bit_depth(im: Image.Image, fmt: str, head: bytes) -> int:
    try:
        if fmt == "heic" and im.info.get("bit_depth"):
            return int(im.info["bit_depth"])
        if fmt == "png" and head.startswith(_PNG_SIGNATURE) and len(head) > 24:
            return int(head[24])
        if fmt == "tiff":
            bps = getattr(im, "tag_v2", {}).get(258)
            if bps:
                return int(max(bps) if isinstance(bps, (tuple, list)) else bps)
    except (TypeError, ValueError):
        pass
    mode = im.mode
    if mode == "1":
        return 1
    if mode.startswith("I;16"):
        return 16
    if mode in ("I", "F"):
        return 32
    return 8


def _to_8bit_gray(img: Image.Image) -> Image.Image:
    """Scale 16/32-bit integer or float grayscale to 8-bit L."""
    arr = np.asarray(img)
    mode = img.mode
    if mode.startswith("I;16"):
        out = (arr.astype(np.uint32) + 128) // 257
    else:
        data = np.nan_to_num(arr.astype(np.float32), nan=0.0, posinf=0.0, neginf=0.0)
        lo, hi = float(data.min()), float(data.max())
        if mode == "F" and lo >= 0 and hi <= 1.0:
            out = data * 255.0
        elif lo >= 0 and hi <= 255:
            out = data
        elif lo >= 0 and hi <= 65535:
            out = data / 257.0
        else:
            out = (data - lo) * (255.0 / (hi - lo)) if hi > lo else np.zeros_like(data)
        out = np.rint(out)
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def _profile(icc: bytes | None) -> ImageCms.ImageCmsProfile | None:
    if not icc:
        return None
    try:
        return ImageCms.ImageCmsProfile(BytesIO(icc))
    except Exception:
        return None


def _profile_space(icc: bytes | None) -> str | None:
    prof = _profile(icc)
    if prof is None:
        return None
    try:
        return prof.profile.xcolor_space.strip().upper()
    except Exception:
        return None


def icc_description(icc: bytes | None) -> str | None:
    """Human-readable ICC profile description ("Display P3"), None when absent/unreadable."""
    prof = _profile(icc)
    if prof is None:
        return None
    try:
        text = ImageCms.getProfileDescription(prof).strip()
    except Exception:
        return None
    return text or None


def _is_srgb(prof: ImageCms.ImageCmsProfile) -> bool:
    try:
        return "srgb" in ImageCms.getProfileDescription(prof).lower()
    except Exception:
        return False


def _normalize_mode(img: Image.Image, icc: bytes | None = None) -> tuple[Image.Image, bytes | None]:
    """Convert any Pillow mode to RGB/RGBA/L/LA; CMYK goes through its ICC profile when present."""
    mode = img.mode
    if mode in ("RGB", "RGBA", "L", "LA"):
        return img, icc
    if mode in ("P", "PA"):
        transparent = mode == "PA" or "transparency" in img.info or (
            img.palette is not None and img.palette.mode == "RGBA")
        return img.convert("RGBA" if transparent else "RGB"), icc
    if mode == "1":
        return img.convert("L"), icc
    if mode == "CMYK":
        if _profile_space(icc) == "CMYK":
            try:
                rgb = ImageCms.profileToProfile(
                    img, _profile(icc), ImageCms.createProfile("sRGB"), outputMode="RGB",
                    renderingIntent=ImageCms.Intent.PERCEPTUAL, flags=ImageCms.Flags.BLACKPOINTCOMPENSATION)
                if rgb is not None:
                    return rgb, None
            except Exception:
                pass
        return img.convert("RGB"), None
    if mode.startswith("I;16") or mode in ("I", "F"):
        return _to_8bit_gray(img), icc
    if mode == "La":
        return img.convert("LA"), icc
    if mode in ("RGBa", "RGBX"):
        return img.convert("RGBA" if mode == "RGBa" else "RGB"), icc
    alpha = "A" in img.getbands() or "transparency" in img.info
    return img.convert("RGBA" if alpha else "RGB"), icc


def _detach(img: Image.Image) -> Image.Image:
    """New plain Image object sharing pixel memory, with no file handle and an empty ``info``.

    Encoders read ``info`` as a fallback (EXIF/ICC/XMP) and Pillow stores per-call settings on the
    image object (``encoderinfo``), so every encode gets its own object: cached images are shared
    between threads.
    """
    img.load()
    try:
        plain = img._new(img.im)  # noqa: SLF001 — avoids a full pixel copy of large images
    except Exception:
        plain = img.copy()
    plain.info = {}
    return plain


# ------------------------------------------------------------------ load
def _open_source(source: str | bytes | bytearray | memoryview | BinaryIO) -> tuple[BinaryIO, bool]:
    if isinstance(source, (bytes, bytearray, memoryview)):
        return BytesIO(bytes(source)), True
    if isinstance(source, (str, os.PathLike)):
        return open(source, "rb"), True  # noqa: SIM115 — closed by load()
    if hasattr(source, "read"):
        if hasattr(source, "seekable") and not source.seekable():
            return BytesIO(source.read()), True
        return source, False
    raise TypeError("load() expects a path, bytes or a binary file object")


def _too_many_pixels(width: int, height: int) -> ImageError:
    return ImageError(
        f"Image is {width}×{height} px ({width * height / 1e6:.0f} MP), above the "
        f"{_max_pixels / 1e6:.0f} MP limit", status=413)


def load(source: str | bytes | BinaryIO) -> Loaded:
    """Decode the first frame, apply EXIF orientation once and normalise the mode.

    Raises ImageError(413) above HEYP_MAX_PIXELS and ImageError(415) for undecodable input.
    """
    _ensure_registered()
    fp, owned = _open_source(source)
    try:
        head = fp.read(_HEAD_BYTES)  # PNG bit depth, AVIF colour boxes
        fp.seek(0)  # Image.open rewinds to 0 as well
        try:
            im = Image.open(fp, formats=_open_formats)
        except Image.DecompressionBombError:
            raise ImageError(f"Image has too many pixels (limit {_max_pixels / 1e6:.0f} MP)", status=413) from None
        except Exception as exc:
            raise ImageError("Unsupported or corrupt image") from exc
        with im:
            return _decode(im, head)
    finally:
        if owned:
            fp.close()


def _decode(im: Image.Image, head: bytes) -> Loaded:
    fmt = _PIL_TO_CANON.get(im.format or "")
    if fmt is None:
        raise ImageError(f"Unsupported image format: {im.format or 'unknown'}")
    width, height = im.size
    if width <= 0 or height <= 0:
        raise ImageError("Image has no pixels")
    if width * height > _max_pixels:
        raise _too_many_pixels(width, height)
    try:
        frames = max(1, int(getattr(im, "n_frames", 1) or 1))
    except Exception:
        frames = 1
    # pillow-heif applies irot/imir itself and resets EXIF orientation to 1 (keeping the original in
    # info). Pillow's TIFF reader transposes on load and drops the tag. Everything else is applied here.
    if fmt == "heic":
        orientation = _valid_orientation(im.info.get("original_orientation"))
    else:
        orientation = _valid_orientation(_safe_getexif(im).get(_TAG_ORIENTATION))
    bit_depth = _bit_depth(im, fmt, head)
    icc = im.info.get("icc_profile") or None
    if icc is not None and not isinstance(icc, bytes):
        icc = None
    if icc is None and fmt in ("heic", "avif") and im.mode in ("RGB", "RGBA"):
        icc = _nclx_profile(im, fmt, head)
    try:
        im.load()
    except Image.DecompressionBombError:
        raise _too_many_pixels(width, height) from None
    except MemoryError:
        raise ImageError("Image is too large to decode", status=413) from None
    except Exception as exc:
        raise ImageError("Could not decode image data (the file may be truncated or corrupt)") from exc

    exif_obj = _safe_getexif(im)
    pending = 1 if fmt == "heic" else _valid_orientation(exif_obj.get(_TAG_ORIENTATION))
    raw_exif = im.info.get("exif") if isinstance(im.info.get("exif"), bytes) else _serialize_exif(exif_obj)
    exif = _clean_exif(raw_exif)
    # Pillow reports 72 dpi for any JPEG whose EXIF lacks resolution tags; trust only real JFIF units.
    dpi_info = im.info if fmt != "jpeg" or im.info.get("jfif_unit") in (1, 2) else {}
    dpi = _dpi_from(dpi_info, exif_obj)

    img: Image.Image = im
    if pending != 1:
        img = img.transpose(_TRANSPOSE[pending])
    img, icc = _normalize_mode(img, icc)
    img = _detach(img)
    return Loaded(image=img, format=fmt, exif=exif, icc_profile=icc, dpi=dpi, frames=frames,
                  bit_depth=bit_depth, orientation=orientation)


# ------------------------------------------------------------------ colour
def to_srgb(img: Image.Image, icc_profile: bytes | None) -> Image.Image:
    """Convert pixels from ``icc_profile`` to sRGB (RGB/RGBA with an RGB profile, L/LA with a gray one).

    No-op without a profile, with an unusable or sRGB profile, or when profile and mode don't match.
    Gray images stay gray: the result is the sRGB rendering's (neutral) gray level.
    """
    if not icc_profile or img.mode not in ("RGB", "RGBA", "L", "LA"):
        return img
    prof = _profile(icc_profile)
    if prof is None or _is_srgb(prof):
        return img
    flags = ImageCms.Flags.BLACKPOINTCOMPENSATION
    try:
        space = prof.profile.xcolor_space.strip().upper()
        if img.mode in ("L", "LA"):
            if space != "GRAY":
                return img
            gray = img.getchannel("L") if img.mode == "LA" else img
            rgb = ImageCms.profileToProfile(gray, prof, ImageCms.createProfile("sRGB"), outputMode="RGB",
                                            renderingIntent=ImageCms.Intent.PERCEPTUAL, flags=flags)
            if rgb is None:
                return img
            out = rgb.convert("L")
            return Image.merge("LA", (out, img.getchannel("A"))) if img.mode == "LA" else out
        if space != "RGB":
            return img
        out = ImageCms.profileToProfile(
            img, prof, ImageCms.createProfile("sRGB"), outputMode=img.mode,
            renderingIntent=ImageCms.Intent.PERCEPTUAL, flags=flags)
    except Exception:  # broken or exotic profile → leave pixels untouched
        return img
    return out if out is not None else img


def flatten(img: Image.Image, color: Any = "#ffffff") -> Image.Image:
    """Composite alpha onto a solid colour (RGBA → RGB, LA → L or RGB); images without alpha are returned as is."""
    if img.mode not in ("RGB", "RGBA", "L", "LA"):
        img, _ = _normalize_mode(img)
    if img.mode not in ("RGBA", "LA"):
        return img
    r, g, b, a = parse_color(color)
    if a == 0:
        r, g, b = 255, 255, 255
    alpha = img.getchannel("A")
    if img.mode == "LA" and r == g == b:
        base, fill = img.getchannel("L"), r
        canvas = Image.new("L", img.size, fill)
    else:
        base = img.convert("RGB")
        canvas = Image.new("RGB", img.size, (r, g, b))
    canvas.paste(base, (0, 0), alpha)
    return canvas


# sRGB IEC61966-2.1 as a compact ICC v2 profile (≈600 bytes; widest viewer compatibility).
_D50 = (0.9642, 1.0, 0.8249)
_SRGB_COLORANTS = ((0.436066, 0.222488, 0.013916), (0.385147, 0.716873, 0.097076), (0.143066, 0.060608, 0.714096))


def _s15f16(value: float) -> bytes:
    return struct.pack(">i", int(round(value * 65536)))


def _srgb_eotf(v: float) -> float:
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def _build_rgb_icc(description: str, colorants: Any, eotf: Any = _srgb_eotf, samples: int = 64) -> bytes:
    """Matrix/TRC display profile: D50-adapted ``colorants`` (rows r, g, b) and a sampled ``eotf`` curve."""
    def xyz(x: float, y: float, z: float) -> bytes:
        return b"XYZ \0\0\0\0" + _s15f16(x) + _s15f16(y) + _s15f16(z)

    text = description.encode("ascii", "replace") + b"\0"
    desc = b"desc\0\0\0\0" + struct.pack(">I", len(text)) + text + struct.pack(">IIHB", 0, 0, 0, 0) + b"\0" * 67
    cprt = b"text\0\0\0\0No copyright, use freely\0"
    points = [min(65535, max(0, int(round(eotf(i / (samples - 1)) * 65535)))) for i in range(samples)]
    curve = b"curv\0\0\0\0" + struct.pack(">I", len(points)) + struct.pack(f">{len(points)}H", *points)
    red, green, blue = (tuple(float(c) for c in row) for row in colorants)
    tags = [(b"desc", desc), (b"cprt", cprt), (b"wtpt", xyz(*_D50)), (b"rXYZ", xyz(*red)),
            (b"gXYZ", xyz(*green)), (b"bXYZ", xyz(*blue)),
            (b"rTRC", curve), (b"gTRC", curve), (b"bTRC", curve)]
    table_len = 4 + 12 * len(tags)
    data, entries, offsets = b"", [], {}
    for sig, payload in tags:
        if payload not in offsets:
            data += b"\0" * (-len(data) % 4)
            offsets[payload] = 128 + table_len + len(data)
            data += payload
        entries.append(struct.pack(">4sII", sig, offsets[payload], len(payload)))
    data += b"\0" * (-len(data) % 4)
    size = 128 + table_len + len(data)
    header = (struct.pack(">I4sI4s4s4s", size, b"\0\0\0\0", 0x02100000, b"mntr", b"RGB ", b"XYZ ")
              + struct.pack(">6H", 2024, 1, 1, 0, 0, 0) + b"acsp" + b"\0" * 24 + struct.pack(">I", 0)
              + _s15f16(_D50[0]) + _s15f16(_D50[1]) + _s15f16(_D50[2]) + b"\0" * 48)
    return header + struct.pack(">I", len(tags)) + b"".join(entries) + data


SRGB_ICC: bytes = _build_rgb_icc("sRGB IEC61966-2.1", _SRGB_COLORANTS)

# ------------------------------------------------------------------ nclx (CICP) colour → ICC
# HEIF/AVIF may describe colour with an nclx box (ITU-T H.273 codes) instead of an ICC profile.
# Primaries: code → (name, (rx, ry), (gx, gy), (bx, by), white xy).
_D65_XY = (0.3127, 0.3290)
_NCLX_PRIMARIES: dict[int, tuple[str, tuple[float, float], tuple[float, float], tuple[float, float],
                                 tuple[float, float]]] = {
    1: ("BT.709", (0.640, 0.330), (0.300, 0.600), (0.150, 0.060), _D65_XY),
    4: ("BT.470M", (0.670, 0.330), (0.210, 0.710), (0.140, 0.080), (0.310, 0.316)),
    5: ("BT.470BG", (0.640, 0.330), (0.290, 0.600), (0.150, 0.060), _D65_XY),
    6: ("BT.601", (0.630, 0.340), (0.310, 0.595), (0.155, 0.070), _D65_XY),
    7: ("SMPTE 240M", (0.630, 0.340), (0.310, 0.595), (0.155, 0.070), _D65_XY),
    8: ("Generic film", (0.681, 0.319), (0.243, 0.692), (0.145, 0.049), (0.310, 0.316)),
    9: ("BT.2020", (0.708, 0.292), (0.170, 0.797), (0.131, 0.046), _D65_XY),
    11: ("DCI-P3", (0.680, 0.320), (0.265, 0.690), (0.150, 0.060), (0.314, 0.351)),
    12: ("Display P3", (0.680, 0.320), (0.265, 0.690), (0.150, 0.060), _D65_XY),
    22: ("EBU 3213", (0.630, 0.340), (0.295, 0.605), (0.155, 0.077), _D65_XY),
}
# Transfer characteristics. Unspecified/reserved and the BT.709 family are rendered with the sRGB curve,
# as browsers do; PQ/HLG (16/18) need tone mapping and are left alone, like other exotic curves.
_NCLX_SRGB_LIKE = frozenset({0, 1, 2, 3, 6, 7, 13, 14, 15})
_NCLX_OTHER_CURVES: dict[int, tuple[str, Any]] = {
    4: ("gamma 2.2", lambda v: v ** 2.2),
    5: ("gamma 2.8", lambda v: v ** 2.8),
    8: ("linear", lambda v: v),
}
_BRADFORD = np.array([[0.8951, 0.2664, -0.1614], [-0.7502, 1.7135, 0.0367], [0.0389, -0.0685, 1.0296]])


def _colorants_from_xy(red: tuple[float, float], green: tuple[float, float], blue: tuple[float, float],
                       white: tuple[float, float]) -> np.ndarray:
    """D50-adapted (Bradford) RGB→XYZ colorants (rows r, g, b) for chromaticities ``red/green/blue/white``."""
    def xyz(xy: tuple[float, float]) -> np.ndarray:
        x, y = xy
        return np.array([x / y, 1.0, (1.0 - x - y) / y])

    prim = np.stack([xyz(red), xyz(green), xyz(blue)], axis=1)
    white_xyz = xyz(white)
    rgb_to_xyz = prim * np.linalg.solve(prim, white_xyz)
    adapt = np.linalg.inv(_BRADFORD) @ np.diag((_BRADFORD @ np.array(_D50)) / (_BRADFORD @ white_xyz)) @ _BRADFORD
    return (adapt @ rgb_to_xyz).T


@functools.lru_cache(maxsize=32)
def _nclx_icc(primaries: Any, transfer: Any) -> bytes | None:
    """ICC profile equivalent to an nclx colour description; None when it is sRGB or not representable."""
    if not isinstance(primaries, int) or not isinstance(transfer, int):
        return None
    if transfer in _NCLX_SRGB_LIKE:
        curve_name, eotf = "", _srgb_eotf
    elif transfer in _NCLX_OTHER_CURVES:
        curve_name, eotf = _NCLX_OTHER_CURVES[transfer]
    else:
        return None
    if primaries == 2 and curve_name:
        primaries = 1  # unspecified primaries with a non-sRGB curve: assume BT.709/sRGB primaries
    if primaries not in _NCLX_PRIMARIES or (primaries == 1 and not curve_name):
        return None
    name, red, green, blue, white = _NCLX_PRIMARIES[primaries]
    try:
        colorants = _colorants_from_xy(red, green, blue, white)
    except (np.linalg.LinAlgError, ZeroDivisionError):  # pragma: no cover — table values are sane
        return None
    label = f"{name}, {curve_name} (nclx)" if curve_name else f"{name} (nclx)"
    return _build_rgb_icc(label, colorants, eotf, samples=1024)


def _box_iter(data: bytes, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    """ISOBMFF boxes in data[start:end] as (type, payload start, payload end); stops at truncation."""
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size, header = struct.unpack_from(">Q", data, pos + 8)[0], 16
        elif size == 0:
            size = end - pos
        if size < header:
            return
        end_pos = pos + size
        yield kind, pos + header, min(end_pos, end)
        if end_pos > end:
            return
        pos = end_pos


def _avif_nclx(head: bytes) -> tuple[int, int] | None:
    """(colour_primaries, transfer_characteristics) of the primary item's nclx 'colr' box in an AVIF.

    Pillow's AVIF plugin exposes only ICC profiles; the nclx box sits in meta/iprp/ipco and is tied to
    the primary item via ipma. Falls back to the first nclx property when there is no association.
    """
    try:
        meta = next(((s, e) for kind, s, e in _box_iter(head, 0, len(head)) if kind == b"meta"), None)
        if meta is None:
            return None
        primary, props, assoc = None, [], {}
        for kind, s, e in _box_iter(head, meta[0] + 4, meta[1]):  # meta is a FullBox
            if kind == b"pitm":
                primary = struct.unpack_from(">H" if head[s] == 0 else ">I", head, s + 4)[0]
            elif kind == b"iprp":
                for sub, s2, e2 in _box_iter(head, s, e):
                    if sub == b"ipco":
                        props = [(k, a, b) for k, a, b in _box_iter(head, s2, e2)]
                    elif sub == b"ipma":
                        assoc.update(_ipma(head, s2, e2))
        nclx = []
        for index, (kind, s, e) in enumerate(props, start=1):
            if kind == b"colr" and e - s >= 11 and head[s:s + 4] == b"nclx":
                nclx.append((index, struct.unpack_from(">HH", head, s + 4)))
        if not nclx:
            return None
        linked = set(assoc.get(primary, ()))
        for index, values in nclx:
            if index in linked:
                return values
        return nclx[0][1]
    except (struct.error, IndexError):
        return None


def _ipma(data: bytes, start: int, end: int) -> dict[int, list[int]]:
    """Item → 1-based property indices from an 'ipma' FullBox payload."""
    version, flags = data[start], int.from_bytes(data[start + 1:start + 4], "big")
    pos = start + 4
    count = struct.unpack_from(">I", data, pos)[0]
    pos += 4
    out: dict[int, list[int]] = {}
    for _ in range(min(count, 4096)):
        if version < 1:
            item = struct.unpack_from(">H", data, pos)[0]
            pos += 2
        else:
            item = struct.unpack_from(">I", data, pos)[0]
            pos += 4
        n = data[pos]
        pos += 1
        indices = []
        for _ in range(n):
            if flags & 1:
                indices.append(struct.unpack_from(">H", data, pos)[0] & 0x7FFF)
                pos += 2
            else:
                indices.append(data[pos] & 0x7F)
                pos += 1
        out[item] = indices
        if pos > end:
            break
    return out


def _nclx_profile(im: Image.Image, fmt: str, head: bytes) -> bytes | None:
    """ICC profile synthesised from a HEIF/AVIF nclx colour box (wide-gamut primaries only)."""
    if fmt == "heic":
        nclx = im.info.get("nclx_profile")
        if not isinstance(nclx, dict):
            return None
        codes = nclx.get("color_primaries"), nclx.get("transfer_characteristics")
    else:
        found = _avif_nclx(head)
        if found is None:
            return None
        codes = found
    if not all(type(c) is int for c in codes):
        return None
    return _nclx_icc(*codes)


# ------------------------------------------------------------------ save
@dataclass
class _Prepared:
    """An image ready for a specific encoder: colour-converted, alpha handled, metadata resolved."""

    image: Image.Image
    fmt: str
    icc: bytes | None
    exif: bytes | None
    dpi: tuple[float, float] | None
    background: tuple[int, int, int, int]


def _dpi_pair(dpi: Any) -> tuple[float, float] | None:
    if dpi is None:
        return None
    if isinstance(dpi, (int, float)) and not isinstance(dpi, bool):
        dpi = (dpi, dpi)
    pair = _valid_dpi(dpi)
    if pair is None:
        raise ValueError(f"Invalid dpi: {dpi!r} (must be between 1 and 100000)")
    return pair


def _output_exif(exif: bytes | Image.Exif | None, fmt: str, dpi: tuple[float, float] | None,
                 strip_metadata: bool, strip_gps: bool) -> bytes | None:
    if fmt in ("gif", "bmp", "pdf"):
        return None
    data = None
    if exif is not None and not strip_metadata:
        data = _parse_exif(exif)
        if data is not None:
            for tag in list(data.keys()):
                if tag in _TIFF_ONLY_TAGS:
                    del data[tag]
            if _TAG_ORIENTATION in data:
                data[_TAG_ORIENTATION] = 1
            if strip_gps:
                data.pop(_TAG_GPS_IFD, None)
    if dpi is not None and (data is not None or fmt in ("heic", "webp", "avif")):
        # Formats without a native resolution field carry DPI in a minimal EXIF block.
        data = data if data is not None else Image.Exif()
        data[_TAG_XRES], data[_TAG_YRES], data[_TAG_RES_UNIT] = float(dpi[0]), float(dpi[1]), 2
    if data is None:
        return None
    raw = _serialize_exif(data)
    if raw and fmt == "jpeg" and len(raw) > _JPEG_MAX_EXIF:
        data.get_ifd(_TAG_EXIF_IFD).pop(_TAG_MAKERNOTE, None)
        raw = _serialize_exif(data)
        if raw and len(raw) > _JPEG_MAX_EXIF:
            raw = None
    return raw


def _has_sub_ifds(exif_bytes: bytes) -> bool:
    exif = _parse_exif(exif_bytes)
    return exif is not None and (_TAG_EXIF_IFD in exif or _TAG_GPS_IFD in exif)


def _libtiff_exif(exif_bytes: bytes) -> bytes | None:
    """IFD0-only EXIF reduced to well-typed tags that libtiff (compressed TIFF) can write safely."""
    exif = _parse_exif(exif_bytes)
    if exif is None:
        return None
    for tag in list(exif.keys()):
        value = exif.get(tag)
        if tag in _LIBTIFF_TEXT_TAGS:
            ok = isinstance(value, str)
        elif tag in (_TAG_XRES, _TAG_YRES):
            ok = (isinstance(value, numbers.Real) and not isinstance(value, bool)
                  and math.isfinite(float(value)) and float(value) > 0)
        elif tag == _TAG_RES_UNIT:
            ok = type(value) is int and value in (1, 2, 3)
        else:
            ok = False
        if not ok:
            del exif[tag]
    return _serialize_exif(exif)


def _prepare(img: Image.Image, fmt: str, *, dpi: Any = None, exif: bytes | Image.Exif | None = None,
             icc_profile: bytes | None = None, strip_metadata: bool = True, strip_gps: bool = True,
             colorspace: str = "srgb", background: Any = "#ffffff") -> _Prepared:
    _ensure_registered()  # the HEIF writer is a plugin
    fmt = canonical_format(fmt)
    if fmt not in OUTPUT_FORMATS:
        raise ValueError(f"Cannot write {fmt.upper()} files (outputs: {', '.join(OUTPUT_FORMATS)})")
    if colorspace not in ("srgb", "keep"):
        raise ValueError(f"Invalid colorspace {colorspace!r} (use 'srgb' or 'keep')")
    bg = parse_color(background)
    dpi_pair = _dpi_pair(dpi)
    img, icc_profile = _normalize_mode(img, icc_profile)
    family = "RGB" if img.mode in ("RGB", "RGBA") else "GRAY"
    if colorspace == "srgb":
        img = to_srgb(img, icc_profile)
        out_icc = SRGB_ICC if family == "RGB" else None
    else:
        out_icc = icc_profile if icc_profile and _profile_space(icc_profile) == family else None
    if fmt in ("jpeg", "bmp", "pdf") and img.mode in ("RGBA", "LA"):
        img = flatten(img, bg)
    if fmt in ("gif", "bmp", "pdf"):
        out_icc = None
    out_exif = _output_exif(exif, fmt, dpi_pair, bool(strip_metadata), bool(strip_gps))
    return _Prepared(image=img, fmt=fmt, icc=out_icc, exif=out_exif, dpi=dpi_pair, background=bg)


def _quantize(img: Image.Image, colors: int) -> Image.Image:
    """Adaptive palette (≤ ``colors``) of the colour channels; alpha is ignored (see _quantize_alpha)."""
    src = img if img.mode == "RGB" else img.convert("RGB")
    return src.quantize(colors=max(2, min(256, colors)), method=Image.Quantize.FASTOCTREE)


def _quantize_alpha(img: Image.Image, colors: int) -> Image.Image:
    """Palette image with per-entry alpha (``info["transparency"]``) keeping alpha 0 and 255 exact.

    FASTOCTREE averages alpha like a fourth colour channel, leaving opaque pixels slightly see-through
    and clear ones slightly visible. Each entry's alpha becomes the median alpha of its pixels; fully
    clear pixels share one clear entry, and fully opaque pixels in a translucent entry get an opaque
    twin of it (or, once the palette is full, the nearest opaque entry).
    """
    colors = max(4, min(256, colors))
    rgba = img.convert("RGBA")
    src_a = np.asarray(rgba.getchannel("A"))
    reserve = max(2, colors // 8)
    quantized = rgba.quantize(colors=colors - reserve, method=Image.Quantize.FASTOCTREE)
    idx = np.asarray(quantized)
    pal = np.array(quantized.getpalette("RGBA") or [], dtype=np.int64).reshape(-1, 4)
    n = int(idx.max()) + 1
    pal = np.vstack([pal[:n], np.zeros((max(0, n - len(pal)), 4), np.int64)])
    # Median source alpha per entry from a joint (entry, alpha) histogram — one O(N) pass.
    hist = np.bincount((idx.astype(np.int64) << 8 | src_a).ravel(), minlength=n * 256)[: n * 256].reshape(n, 256)
    cum = hist.cumsum(axis=1)
    counts = cum[:, -1]
    median = (cum >= ((counts + 1) // 2)[:, None]).argmax(axis=1)
    pal[:, 3] = np.where(counts > 0, median, pal[:, 3])
    entry_alpha = pal[:, 3]
    remap = np.arange(n + reserve, dtype=np.uint8)
    out_idx = idx.copy()
    extra: list[list[int]] = []
    clear_wrong = (src_a == 0) & (entry_alpha[idx] != 0)
    if clear_wrong.any():
        clear = np.flatnonzero(entry_alpha == 0)
        if clear.size:
            out_idx[clear_wrong] = clear[0]
        else:
            out_idx[clear_wrong] = n
            extra.append([0, 0, 0, 0])
    opaque_wrong = (src_a == 255) & (entry_alpha[idx] != 255)
    if opaque_wrong.any():
        need = np.bincount(idx[opaque_wrong], minlength=n)
        order = [int(i) for i in np.argsort(-need, kind="stable") if need[i]]
        slots = reserve - len(extra)
        for i in order[:slots]:
            remap[i] = n + len(extra)
            extra.append([*pal[i, :3].tolist(), 255])
        rest = order[slots:]
        if rest:
            opaque_rgb = [(j, pal[j, :3]) for j in range(n) if entry_alpha[j] == 255 and counts[j]]
            opaque_rgb += [(n + k, np.array(e[:3])) for k, e in enumerate(extra) if e[3] == 255]
            cand_idx = np.array([j for j, _ in opaque_rgb])
            cand_rgb = np.array([c for _, c in opaque_rgb], dtype=np.int64)
            for i in rest:
                dist = ((cand_rgb - pal[i, :3]) ** 2).sum(axis=1)
                remap[i] = cand_idx[int(dist.argmin())]
        out_idx[opaque_wrong] = remap[idx[opaque_wrong]]
    table = np.vstack([pal, np.array(extra, dtype=np.int64).reshape(-1, 4)]).clip(0, 255).astype(np.uint8)
    out = Image.fromarray(out_idx.astype(np.uint8), "P")
    out.putpalette(table[:, :3].tobytes(), "RGB")
    out.info["transparency"] = table[:, 3].tobytes()
    return out


def _gif_image(img: Image.Image, background: tuple[int, int, int, int], colors: int) -> tuple[Image.Image, int | None]:
    """Palette image for GIF; alpha becomes one transparent index (alpha < 128)."""
    if img.mode in ("RGBA", "LA"):
        alpha = img.getchannel("A")
        pal = _quantize(flatten(img, background).convert("RGB"), min(colors, 255))
        palette = pal.getpalette() or []
        pal.putpalette(palette + [0] * (768 - len(palette)))
        mask = alpha.point([255] * 128 + [0] * 128)
        if mask.getbbox() is None:
            return pal, None
        pal.paste(255, (0, 0), mask)
        return pal, 255
    if img.mode == "L" and colors >= 256:
        return img, None
    return _quantize(img, colors), None


def _heic_quality(quality: int) -> int:
    """User quality → libheif/x265 quality, whose scale is much steeper (its q90 is near-lossless).

    Piecewise linear through 1→1, 90→70, 100→100, so UI 90 gives files about as large as JPEG q90
    while the end points and the ordering stay intact.
    """
    if quality <= 90:
        return max(1, round(quality * 70 / 90))
    return 70 + (quality - 90) * 3


_maxblock_lock = threading.Lock()
_maxblock = {"users": 0, "base": ImageFile.MAXBLOCK}
_JPEG_SAMPLES_PER_PX = {"4:4:4": 3.0, "4:2:2": 2.0, "4:2:0": 1.5}


def _jpeg_bufsize(img: Image.Image, subsampling: str, params: dict[str, Any]) -> int:
    """Upper bound for an optimised/progressive JPEG's size.

    Uniform noise at q100 needs ≈1 byte per sample (Pillow guesses 1–2 bytes per *pixel*, too little
    for noisy photos at high quality or 4:4:4), so twice the sample count leaves ample headroom.
    """
    per_px = 1.0 if img.mode == "L" else _JPEG_SAMPLES_PER_PX[subsampling]
    metadata = len(params.get("icc_profile") or b"") + len(params.get("exif") or b"")
    return int(2 * per_px * img.width * img.height) + 2 * metadata + 65536


@contextmanager
def _jpeg_buffer(nbytes: int) -> Iterator[None]:
    """Make Pillow's encoder buffer at least ``nbytes`` while the block runs.

    With optimize/progressive libjpeg emits the whole file at once and cannot suspend, so a buffer
    smaller than the output fails ("Suspension not allowed here" on stderr + OSError). The size comes
    from the module global ImageFile.MAXBLOCK: it is raised while any JPEG encode is running and
    restored when the last one finishes. Concurrent encoders only ever see a larger buffer, which is
    allocated lazily, so nothing is serialised.
    """
    with _maxblock_lock:
        if _maxblock["users"] == 0:
            _maxblock["base"] = ImageFile.MAXBLOCK
        _maxblock["users"] += 1
        ImageFile.MAXBLOCK = max(ImageFile.MAXBLOCK, nbytes)
    try:
        yield
    finally:
        with _maxblock_lock:
            _maxblock["users"] -= 1
            if _maxblock["users"] == 0:
                ImageFile.MAXBLOCK = _maxblock["base"]


def _encode(p: _Prepared, *, quality: int = 90, lossless: bool = False, progressive: bool = True,
            chroma: str = "auto", colors: int | None = None, optimize: bool = False, fast: bool = False) -> bytes:
    """Encode a prepared image.

    ``colors`` palettises png/tiff/bmp/gif, ``optimize`` makes slower/smaller PNGs and ``fast`` trades a
    little AVIF/HEIC efficiency for ~1.5–6× speed (used by the target-size search).
    """
    img, fmt = _detach(p.image), p.fmt
    if isinstance(quality, bool) or not isinstance(quality, (int, float)) or not 1 <= quality <= 100:
        raise ValueError("quality must be between 1 and 100")
    quality = int(round(quality))
    if chroma not in ("auto", "444", "422", "420"):
        raise ValueError(f"Invalid chroma {chroma!r} (use auto, 444, 422 or 420)")
    buf = BytesIO()
    params: dict[str, Any] = {}
    if p.icc:
        params["icc_profile"] = p.icc
    if p.exif:
        params["exif"] = p.exif
    try:
        if fmt == "jpeg":
            if max(img.size) > _JPEG_MAX_SIDE:
                raise ValueError(f"JPEG supports at most {_JPEG_MAX_SIDE} px per side")
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            sub = {"444": "4:4:4", "422": "4:2:2", "420": "4:2:0"}.get(chroma) or (
                "4:4:4" if quality >= 90 else "4:2:0")
            params.update(quality=quality, optimize=True, progressive=bool(progressive), subsampling=sub)
            if p.dpi:
                params["dpi"] = (int(round(p.dpi[0])), int(round(p.dpi[1])))
            with _jpeg_buffer(_jpeg_bufsize(img, sub, params)):
                img.save(buf, "JPEG", **params)
        elif fmt == "png":
            if colors:
                img = _quantize_alpha(img, colors) if has_alpha(img) else _quantize(img, colors)
            if p.dpi:
                params["dpi"] = p.dpi
            # zlib level 9 is ~9× slower than 6 on large truecolour images for ~7 % gain; on palette
            # images (1 byte/px) it is cheap.
            optimize = bool(optimize) and (img.mode == "P" or img.width * img.height <= _PNG_OPTIMIZE_MAX_PIXELS)
            img.save(buf, "PNG", optimize=optimize, compress_level=9 if optimize else 6, **params)
        elif fmt == "webp":
            if max(img.size) > _WEBP_MAX_SIDE:
                raise ValueError(f"WebP supports at most {_WEBP_MAX_SIDE} px per side")
            # On big frames libwebp's method ≥ 3 re-encodes the whole frame whenever the 512 KB mode
            # partition overflows (high quality): 4–8× slower. Method 2 never retries.
            method = 2 if not lossless and img.width * img.height > _WEBP_FAST_ABOVE else 4
            webp = dict(quality=80 if lossless else quality, lossless=bool(lossless), **params)
            try:
                img.save(buf, "WEBP", method=method, **webp)
            except (ValueError, OSError, RuntimeError):
                if method == 4:
                    raise
                buf = BytesIO()  # method < 3 reports a partition overflow instead of retrying
                img.save(buf, "WEBP", method=4, **webp)
        elif fmt == "avif":
            # Pillow's AVIF writer has no true lossless mode: q100 + 4:4:4 is the closest (±2–3 levels).
            if lossless:
                params.update(quality=100, subsampling="4:4:4")
            else:
                params["quality"] = quality
                sub = {"444": "4:4:4", "422": "4:2:2", "420": "4:2:0"}.get(chroma)
                if sub and img.mode != "L":
                    params["subsampling"] = sub
            img.save(buf, "AVIF", speed=8 if fast else 6, **params)
        elif fmt == "heic":
            if not FEATURES.get("heic"):
                raise ValueError("HEIC output is not available on this server")
            params.setdefault("exif", None)
            params["xmp"] = None
            if lossless:
                params.update(quality=-1, chroma="444")
                if img.mode in ("RGB", "RGBA"):
                    params["matrix_coefficients"] = 0  # identity (RGB) → bit-exact
            else:
                params["quality"] = _heic_quality(quality)
                if chroma != "auto":
                    params["chroma"] = chroma
            tiled = img.width * img.height > _HEIC_GRID_ABOVE and img.mode in ("RGB", "L")
            if tiled:
                params["tile_size"] = 512
            if fast or tiled:
                # libheif's x265 default preset is "slow": ~1.5× slower for ~8 % smaller files.
                params["enc_params"] = {"preset": "medium"}
            img.save(buf, "HEIF", **params)
        elif fmt == "tiff":
            if colors and has_alpha(img):
                # The TIFF writer keeps only an RGB colormap: reduce the colours, keep the exact alpha.
                alpha = img.getchannel("A")
                img = _quantize(img, colors).convert("RGB" if img.mode == "RGBA" else "L")
                img.putalpha(alpha)
            elif colors:
                img = _quantize(img, colors)
            if p.dpi:
                params["dpi"] = p.dpi
            # libtiff (used for compressed TIFF) cannot write the Exif/GPS sub-IFD pointers, so such
            # files use Pillow's own uncompressed writer. Everything else goes through libtiff, which
            # must only ever see allow-listed IFD0 tags (others can segfault the process).
            if p.exif and _has_sub_ifds(p.exif):
                compression = "raw"
            else:
                compression = "tiff_lzw"
                if p.exif:
                    params["exif"] = _libtiff_exif(p.exif)
                    if not params["exif"]:
                        del params["exif"]
            img.save(buf, "TIFF", compression=compression, **params)
        elif fmt == "bmp":
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            if colors:
                img = _quantize(img, colors)
            img.save(buf, "BMP", dpi=p.dpi or (96.0, 96.0))
        elif fmt == "gif":
            pal, transparent = _gif_image(img, p.background, colors or 256)
            extra = {"transparency": transparent} if transparent is not None else {}
            pal.save(buf, "GIF", optimize=True, **extra)
        elif fmt == "pdf":
            page = img if img.mode == "RGB" else img.convert("RGB")
            dpi = p.dpi or (300.0, 300.0)
            page.save(buf, "PDF", dpi=dpi, quality=quality)
        else:  # pragma: no cover — _prepare validated fmt
            raise ValueError(f"Cannot write {fmt}")
    except ValueError:
        raise
    except (OSError, RuntimeError, SyntaxError, struct.error) as exc:
        raise ValueError(f"Could not encode {fmt.upper()}: {exc}") from exc
    return buf.getvalue()


def save(img: Image.Image, fmt: str, *, quality: int = 90, lossless: bool = False, progressive: bool = True,
         chroma: str = "auto", dpi: Any = None, exif: bytes | Image.Exif | None = None,
         icc_profile: bytes | None = None, strip_metadata: bool = True, strip_gps: bool = True,
         colorspace: str = "srgb", background: Any = "#ffffff") -> bytes:
    """Encode ``img`` as ``fmt`` (jpeg png webp avif heic tiff bmp gif pdf) and return the bytes.

    colorspace="srgb" converts from ``icc_profile`` and embeds sRGB; "keep" embeds the original
    profile. Alpha is flattened onto ``background`` for jpeg/bmp/pdf; gif keeps 1-bit transparency.
    ``strip_metadata`` drops EXIF (GPS always goes with it); ``strip_gps`` removes GPS from kept EXIF.
    """
    prepared = _prepare(img, fmt, dpi=dpi, exif=exif, icc_profile=icc_profile, strip_metadata=strip_metadata,
                        strip_gps=strip_gps, colorspace=colorspace, background=background)
    return _encode(prepared, quality=quality, lossless=lossless, progressive=progressive, chroma=chroma)


def save_pdf(images: list[Image.Image], *, dpi: Any = 300, background: Any = "#ffffff") -> bytes:
    """One PDF page per image (alpha flattened, RGB), page size = pixels / dpi."""
    if not images:
        raise ValueError("No images to put in the PDF")
    pair = _dpi_pair(dpi if dpi else 300)
    pages = []
    for img in images:
        page, _ = _normalize_mode(img)
        page = flatten(page, background)
        pages.append(page if page.mode == "RGB" else page.convert("RGB"))
    pages = [_detach(page) for page in pages]
    buf = BytesIO()
    try:
        pages[0].save(buf, "PDF", save_all=True, append_images=pages[1:], dpi=pair, quality=92)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Could not write PDF: {exc}") from exc
    return buf.getvalue()


# ------------------------------------------------------------------ preview
def preview(img: Image.Image, icc_profile: bytes | None = None, max_side: int = 1600) -> tuple[bytes, str]:
    """sRGB display copy ≤ ``max_side`` px: (JPEG q82 bytes, "image/jpeg") or PNG when alpha is present."""
    try:
        max_side = int(max_side)
    except (TypeError, ValueError):
        raise ValueError("max_side must be an integer") from None
    if max_side < 1:
        raise ValueError("max_side must be at least 1")
    img, icc_profile = _normalize_mode(img, icc_profile)
    width, height = img.size
    factor = min(1.0, max_side / max(width, height))
    if factor < 1.0:
        img = img.resize((max(1, round(width * factor)), max(1, round(height * factor))),
                         Image.Resampling.LANCZOS, reducing_gap=3.0)
    img = _detach(to_srgb(img, icc_profile))
    buf = BytesIO()
    if has_alpha(img):
        img.save(buf, "PNG", compress_level=3)
        return buf.getvalue(), "image/png"
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB" if img.mode == "RGBA" else "L")
    img.save(buf, "JPEG", quality=82)
    return buf.getvalue(), "image/jpeg"


def _replace_image(p: _Prepared, image: Image.Image) -> _Prepared:
    """Same encoding settings for a different (e.g. downscaled) image — used by compress."""
    return replace(p, image=image)
