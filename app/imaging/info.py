"""Human-readable file description (PLAN.md §3 FileInfo, §4 info.describe)."""
from __future__ import annotations

import math
from typing import Any

from PIL import ExifTags, Image

from . import io

_IFD0, _EXIF_IFD, _GPS_IFD, _INTEROP_IFD = 0, 0x8769, 0x8825, 0xA005
_POINTER_TAGS = frozenset({_EXIF_IFD, _GPS_IFD, _INTEROP_IFD, 0x014A})
_MAX_STRING = 512
_MAX_LIST = 64


def _text(value: Any) -> str | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, str):
        return None
    text = value.replace("\x00", "").strip()
    return text[:_MAX_STRING] or None


def _number(value: Any) -> float | None:
    if isinstance(value, (tuple, list)):
        value = value[0] if value else None
    try:
        number = float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return number if math.isfinite(number) else None


def _clean(value: Any) -> Any:
    """JSON-safe readable value: IFDRational → float, short bytes → text/hex, None when unreadable."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 6) if math.isfinite(value) else None
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, bytes):
        if not value:
            return None
        if len(value) <= 64 and all(32 <= b < 127 for b in value.rstrip(b"\x00")):
            return _text(value)
        return value.hex() if len(value) <= 16 else None
    if isinstance(value, (tuple, list)):
        if len(value) > _MAX_LIST:
            return None
        items = [_clean(v) for v in value]
        if any(v is None for v in items):
            return None
        return items[0] if len(items) == 1 else items
    if hasattr(value, "numerator") and hasattr(value, "denominator"):
        try:
            if value.denominator == 0:
                return None
            return _clean(float(value))
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    return None


def _exif_parts(exif_bytes: bytes | None) -> tuple[dict[int, Any], dict[int, Any], dict[int, Any]]:
    """(IFD0, Exif IFD, GPS IFD) as raw tag dicts; empty on any parsing problem."""
    if not exif_bytes:
        return {}, {}, {}
    exif = Image.Exif()
    try:
        exif.load(exif_bytes)
    except Exception:
        return {}, {}, {}

    def ifd(tag: int) -> dict[int, Any]:
        try:
            return dict(exif.get_ifd(tag))
        except Exception:
            return {}

    try:
        base = dict(exif)
    except Exception:
        base = {}
    return base, ifd(_EXIF_IFD), ifd(_GPS_IFD)


def _exposure(value: Any) -> str | None:
    seconds = _number(value)
    if seconds is None or seconds <= 0:
        return None
    if seconds < 1:
        return f"1/{round(1 / seconds)}"
    return f"{seconds:g}"


def _camera(ifd0: dict[int, Any], exif_ifd: dict[int, Any]) -> dict[str, Any] | None:
    iso = exif_ifd.get(0x8827)
    if isinstance(iso, (tuple, list)):
        iso = iso[0] if iso else None
    focal = _number(exif_ifd.get(0x920A))
    f_number = _number(exif_ifd.get(0x829D))
    camera = {
        "make": _text(ifd0.get(0x010F)),
        "model": _text(ifd0.get(0x0110)),
        "lens": _text(exif_ifd.get(0xA434)),
        "datetime": _text(exif_ifd.get(0x9003)) or _text(ifd0.get(0x0132)),
        "iso": int(iso) if isinstance(iso, int) and iso > 0 else None,
        "exposure": _exposure(exif_ifd.get(0x829A)),
        "f_number": round(f_number, 1) if f_number and f_number > 0 else None,
        "focal_length_mm": round(focal, 1) if focal and focal > 0 else None,
    }
    return camera if any(v is not None for v in camera.values()) else None


def _dms(value: Any) -> float | None:
    if not isinstance(value, (tuple, list)) or not 1 <= len(value) <= 3:
        return None
    parts = [_number(v) for v in value]
    if any(p is None for p in parts):
        return None
    parts += [0.0] * (3 - len(parts))
    return parts[0] + parts[1] / 60 + parts[2] / 3600


def _gps(gps_ifd: dict[int, Any]) -> dict[str, float] | None:
    lat, lon = _dms(gps_ifd.get(2)), _dms(gps_ifd.get(4))
    if lat is None or lon is None:
        return None
    if (_text(gps_ifd.get(1)) or "N").upper().startswith("S"):
        lat = -lat
    if (_text(gps_ifd.get(3)) or "E").upper().startswith("W"):
        lon = -lon
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return {"lat": round(lat, 6), "lon": round(lon, 6)}


def _full_exif(ifd0: dict[int, Any], exif_ifd: dict[int, Any], gps_ifd: dict[int, Any],
               orientation: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for tags, names in ((ifd0, ExifTags.TAGS), (exif_ifd, ExifTags.TAGS), (gps_ifd, ExifTags.GPSTAGS)):
        for tag, raw in tags.items():
            if tag in _POINTER_TAGS and names is ExifTags.TAGS:
                continue
            name = names.get(tag)
            if not name:
                continue
            try:
                value = _clean(raw)
            except Exception:
                value = None
            if value is not None and value != [] and value != "":
                out[name] = value
    if "Orientation" in out or orientation != 1:
        out["Orientation"] = orientation  # the file's own value, not the reset copy we keep
    return out


def _dpi_json(dpi: tuple[float, float] | None) -> list[float] | None:
    if not dpi:
        return None
    return [int(v) if float(v).is_integer() else round(float(v), 2) for v in dpi]


def describe(loaded: io.Loaded, *, name: str, size_bytes: int, full_exif: bool = False) -> dict:
    """FileInfo (minus ``id``) for a loaded image; ``exif`` is included only when ``full_exif``."""
    img = loaded.image
    width, height = img.size
    ifd0, exif_ifd, gps_ifd = _exif_parts(loaded.exif)
    dpi = _dpi_json(loaded.dpi)
    print_size = [round(width / dpi[0], 2), round(height / dpi[1], 2)] if dpi else None
    try:
        mime = io.mime_for(loaded.format)
    except ValueError:
        mime = "application/octet-stream"
    info: dict[str, Any] = {
        "name": name,
        "format": loaded.format,
        "mime": mime,
        "size_bytes": int(size_bytes),
        "width": width,
        "height": height,
        "mode": img.mode,
        "has_alpha": io.has_alpha(img),
        "frames": loaded.frames,
        "bit_depth": loaded.bit_depth,
        "dpi": dpi,
        "orientation": loaded.orientation,
        "icc": io.icc_description(loaded.icc_profile),
        "print_size_in": print_size,
        "camera": _camera(ifd0, exif_ifd),
        "gps": _gps(gps_ifd),
    }
    if full_exif:
        try:
            info["exif"] = _full_exif(ifd0, exif_ifd, gps_ifd, loaded.orientation)
        except Exception:  # odd EXIF never breaks the info endpoint
            info["exif"] = {}
    return info
