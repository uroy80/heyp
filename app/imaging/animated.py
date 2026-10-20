"""Animated GIF / WebP / APNG: decode every frame, apply per-frame ops, encode an animation.

The pipeline uses this when every op of a request has a ``frame_apply``: ``load`` →
``apply`` → ``encode``. A frame bomb (critic blocking #4: a 112 KB GIF with a 7000² canvas and 5000
frames) is refused before any frame is decoded: frames ≤ MAX_FRAMES and frames × canvas pixels ≤
HEYP_MAX_ANIM_PIXELS (default 400 MP). PSD "frames" are layers and are never iterated (only GIF,
WebP and PNG count as animations).
"""
from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, BinaryIO

from PIL import Image

from . import io

__all__ = [
    "MAX_FRAMES", "ANIMATED_FORMATS", "ANIMATION_OUTPUTS", "Animation", "max_anim_pixels", "check_limits", "probe",
    "is_animated", "load", "apply", "encode", "DEFAULT_DURATION_MS",
]

MAX_FRAMES = 1000
DEFAULT_MAX_ANIM_PIXELS = 400_000_000
DEFAULT_DURATION_MS = 100          # what browsers use for a GIF frame without (or with a 0/10 ms) delay
BROWSER_MIN_GIF_MS = 10            # browsers show GIF delays of 0–10 ms (0–1 cs) at DEFAULT_DURATION_MS
MIN_GIF_DURATION_MS = 20           # GIF delays are centiseconds; < 20 ms is clamped by browsers anyway
MAX_DURATION_MS = 655_350          # GIF delay field limit (65535 cs)
ANIMATED_FORMATS = ("gif", "webp", "png")
ANIMATION_OUTPUTS = ("gif", "webp", "png")   # png → APNG
_PIL_FORMATS = {"gif": ["GIF"], "webp": ["WEBP"], "png": ["PNG"]}


def max_anim_pixels() -> int:
    """HEYP_MAX_ANIM_PIXELS: total decoded pixels allowed across all frames of one animation."""
    raw = os.environ.get("HEYP_MAX_ANIM_PIXELS", "").strip().replace("_", "")
    try:
        value = int(float(raw)) if raw else DEFAULT_MAX_ANIM_PIXELS
    except ValueError:
        value = DEFAULT_MAX_ANIM_PIXELS
    return value if value > 0 else DEFAULT_MAX_ANIM_PIXELS


def check_limits(n_frames: int, width: int, height: int) -> None:
    """Raise ImageError(413) for an animation that would decode too many frames or pixels."""
    if n_frames > MAX_FRAMES:
        raise io.ImageError(f"Animation has {n_frames} frames (limit {MAX_FRAMES})", status=413)
    total = n_frames * width * height
    limit = max_anim_pixels()
    if total > limit:
        raise io.ImageError(
            f"Animation is too large to edit frame by frame: {n_frames} frames of {width}×{height} px "
            f"({total / 1e6:.0f} MP decoded, limit {limit / 1e6:.0f} MP)", status=413)


@dataclass
class Animation:
    frames: list[Image.Image]          # full-canvas frames, all the same size, mode RGBA or RGB
    durations: list[int]               # ms per frame
    loop: int | None = 0               # 0 = forever, None = play once (GIF without a loop extension)
    format: str = "gif"                # source format
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def size(self) -> tuple[int, int]:
        return self.frames[0].size

    def __len__(self) -> int:
        return len(self.frames)


def _open(source: str | bytes | BinaryIO) -> tuple[Image.Image, BinaryIO | None]:
    if isinstance(source, (bytes, bytearray, memoryview)):
        fp: BinaryIO | None = BytesIO(bytes(source))
        return Image.open(fp, formats=["GIF", "WEBP", "PNG"]), fp
    if isinstance(source, (str, os.PathLike)):
        return Image.open(source, formats=["GIF", "WEBP", "PNG"]), None
    return Image.open(source, formats=["GIF", "WEBP", "PNG"]), None


def _head(source: str | bytes | BinaryIO, n: int = 16) -> bytes:
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source[:n])
    if isinstance(source, (str, os.PathLike)):
        with open(source, "rb") as fh:
            return fh.read(n)
    pos = source.tell()
    try:
        return source.read(n)
    finally:
        source.seek(pos)


def _could_be_animated(head: bytes) -> bool:
    """GIF, WebP or PNG magic (anything else — JPEG, PSD whose "frames" are layers … — is a still)."""
    return head[:6] in (b"GIF87a", b"GIF89a") or (head[:4] == b"RIFF" and head[8:12] == b"WEBP") \
        or head[:8] == b"\x89PNG\r\n\x1a\n"


def probe(source: str | bytes | BinaryIO) -> tuple[str, int, int, int] | None:
    """(format, frames, width, height) of a GIF/WebP/PNG without decoding pixels; None otherwise."""
    try:
        im, fp = _open(source)
    except Exception:
        return None
    try:
        with im:
            fmt = io._PIL_TO_CANON.get(im.format or "")  # noqa: SLF001
            if fmt not in ANIMATED_FORMATS:
                return None
            try:
                n = max(1, int(getattr(im, "n_frames", 1) or 1))
            except Exception:
                n = 1
            return fmt, n, im.width, im.height
    finally:
        if fp is not None:
            fp.close()


def is_animated(source: str | bytes | BinaryIO) -> bool:
    found = probe(source)
    return bool(found and found[1] > 1)


def _duration(value: Any, fmt: str | None = None) -> int:
    """A frame's delay in ms as viewers show it: missing/0 → DEFAULT_DURATION_MS, and for GIF sources
    also 1 cs (10 ms), which every browser plays at 100 ms (so an edit doesn't speed the animation up)."""
    try:
        ms = int(round(float(value)))
    except (TypeError, ValueError):
        return DEFAULT_DURATION_MS
    if ms <= 0 or (fmt == "gif" and ms <= BROWSER_MIN_GIF_MS):
        return DEFAULT_DURATION_MS
    return ms


def load(source: str | bytes | BinaryIO, *, check_cancel: Callable[[], None] | None = None) -> Animation | None:
    """Every frame of an animated GIF/WebP/APNG as full-canvas RGBA/RGB images, or None for a still image.

    Limits are checked from the header first (ImageError 413). ``check_cancel`` is called between
    frames so a job can stop a long decode.
    """
    io._ensure_registered()  # noqa: SLF001 — pixel limit + plugins
    try:
        if not _could_be_animated(_head(source)):
            return None
    except OSError as exc:
        raise io.ImageError("Unsupported or corrupt image") from exc
    try:
        im, fp = _open(source)
    except Image.DecompressionBombError:
        raise io.ImageError("Image has too many pixels", status=413) from None
    except Exception as exc:
        raise io.ImageError("Unsupported or corrupt image") from exc
    try:
        with im:
            fmt = io._PIL_TO_CANON.get(im.format or "")  # noqa: SLF001
            if fmt not in ANIMATED_FORMATS:
                return None
            n = max(1, int(getattr(im, "n_frames", 1) or 1))
            if n < 2:
                return None
            width, height = im.size
            if width * height > io.max_pixels():
                raise io._too_many_pixels(width, height)  # noqa: SLF001
            check_limits(n, width, height)
            loop = im.info.get("loop")
            if fmt == "gif":
                loop = None if loop is None else int(loop)
            else:
                loop = int(loop or 0)
            frames: list[Image.Image] = []
            durations: list[int] = []
            transparent = False
            for index in range(n):
                if check_cancel is not None:
                    check_cancel()
                im.seek(index)
                frame = im.convert("RGBA")
                if frame.size != (width, height):  # defensive: Pillow composites onto the canvas
                    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                    canvas.paste(frame, (0, 0))
                    frame = canvas
                if not transparent and frame.getchannel("A").getextrema()[0] < 255:
                    transparent = True
                frames.append(frame)
                durations.append(_duration(im.info.get("duration"), fmt))
    except io.ImageError:
        raise
    except Image.DecompressionBombError:
        raise io.ImageError("Image has too many pixels", status=413) from None
    except (OSError, ValueError, EOFError, SyntaxError) as exc:
        raise io.ImageError("Could not decode every frame of the animation (the file may be corrupt)") from exc
    finally:
        if fp is not None:
            fp.close()
    if not transparent:
        frames = [f.convert("RGB") for f in frames]
    return Animation(frames=frames, durations=durations, loop=loop, format=fmt)


def apply(anim: Animation, fn: Callable[[Image.Image], Image.Image], *,
          check_cancel: Callable[[], None] | None = None) -> Animation:
    """Apply ``fn`` to every frame; every result must have the same size (geometry is per animation)."""
    out: list[Image.Image] = []
    size: tuple[int, int] | None = None
    for frame in anim.frames:
        if check_cancel is not None:
            check_cancel()
        result = fn(frame)
        if size is None:
            size = result.size
        elif result.size != size:
            raise ValueError("This edit gives frames of different sizes; it can't be applied to an animation")
        out.append(result)
    return Animation(frames=out, durations=list(anim.durations), loop=anim.loop, format=anim.format,
                     info=dict(anim.info))


def _gif_frames(frames: Sequence[Image.Image], background: Any, colors: int) -> tuple[list[Image.Image], bool]:
    """Palette frames for GIF; alpha < 128 becomes each frame's transparent index (see io._gif_image)."""
    bg = io.parse_color(background) if not isinstance(background, tuple) else background
    out, any_transparent = [], False
    for frame in frames:
        img = frame if frame.mode in ("RGB", "RGBA", "L", "LA") else frame.convert("RGBA")
        pal, transparent = io._gif_image(img, bg, colors)  # noqa: SLF001
        if pal.mode != "P":
            pal = pal.convert("P")
        if transparent is not None:
            pal.info["transparency"] = transparent
            any_transparent = True
        else:
            pal.info.pop("transparency", None)
        out.append(pal)
    return out, any_transparent


def encode(frames: Sequence[Image.Image], durations: Sequence[int], loop: int | None = 0, fmt: str = "gif", *,
           quality: int = 90, lossless: bool = False, background: Any = "#ffffff", colors: int = 256,
           optimize: bool = True) -> bytes:
    """Encode an animation as GIF, animated WebP or APNG (``fmt`` "png").

    Frames must share one size. ``loop`` 0 loops forever, None plays once, n repeats n extra times
    (GIF/APNG semantics). GIF frames get their own palette (≤ ``colors``) and 1-bit transparency;
    with ``optimize`` the GIF is passed through gifsicle ``-O3`` (lossless) when installed.
    """
    fmt = io.canonical_format(fmt)
    if fmt not in ANIMATION_OUTPUTS:
        raise ValueError(f"Animations can be saved as GIF, WebP or PNG (APNG), not {fmt.upper()}")
    if not frames:
        raise ValueError("No frames to encode")
    if len(frames) > MAX_FRAMES:
        raise ValueError(f"Too many frames ({len(frames)}, max {MAX_FRAMES})")
    if len(durations) != len(frames):
        raise ValueError("durations must have one entry per frame")
    size = frames[0].size
    if any(f.size != size for f in frames):
        raise ValueError("All frames of an animation must have the same size")
    if isinstance(quality, bool) or not 1 <= int(quality) <= 100:
        raise ValueError("quality must be between 1 and 100")
    if loop is not None and not 0 <= int(loop) <= 65535:
        raise ValueError("loop must be between 0 (forever) and 65535")
    if fmt == "webp" and max(size) > 16383:
        raise ValueError("WebP supports at most 16383 px per side")
    ms = [min(MAX_DURATION_MS, max(1, int(d))) for d in durations]
    buf = BytesIO()
    try:
        if fmt == "gif":
            ms = [max(MIN_GIF_DURATION_MS, int(round(d / 10.0)) * 10) for d in ms]
            pal, transparent = _gif_frames(frames, background, max(2, min(256, int(colors))))
            params: dict[str, Any] = {"save_all": True, "append_images": pal[1:], "duration": ms,
                                      "disposal": 2 if transparent else 1, "optimize": False}
            if loop is not None:
                params["loop"] = int(loop)
            pal[0].save(buf, "GIF", **params)
            data = buf.getvalue()
            if optimize:
                from . import compress

                if compress.tool_available("gifsicle"):
                    try:
                        data = compress.gifsicle(data)
                    except (ValueError, compress.ToolUnavailable):
                        pass
            return data
        rgba = [f if f.mode in ("RGB", "RGBA") else f.convert("RGBA") for f in frames]
        if fmt == "webp":
            params = {"save_all": True, "append_images": rgba[1:], "duration": ms, "loop": 1 if loop is None else int(loop),
                      "quality": int(quality), "lossless": bool(lossless), "method": 4,
                      "background": (0, 0, 0, 0)}
            rgba[0].save(buf, "WEBP", **params)
        else:
            params = {"save_all": True, "append_images": rgba[1:], "duration": ms, "loop": 1 if loop is None else int(loop),
                      "disposal": 1, "blend": 0}
            rgba[0].save(buf, "PNG", **params)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Could not encode the animation: {exc}") from exc
    return buf.getvalue()
