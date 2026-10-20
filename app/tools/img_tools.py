"""heyp job tools and editor assets (PLAN.md §8).

Job tools (``POST /api/jobs``; progress over SSE; results are chainable files):

* ``img.make_gif`` (lane heavy) — the job's images, in ``order`` (indices into ``file_ids``; default
  the upload order), become the frames of one animated GIF or WebP. The canvas is the first frame's
  size (``size`` = its longest side, 0 = as is); other frames are fitted onto it (``fit`` contain with
  ``background`` bars, or cover). An animation larger than HEYP_MAX_ANIM_PIXELS in total is scaled
  down with a warning. Output ``<first stem>_make_gif.gif|webp``; ``data`` = {frames, width, height,
  format, delay_ms, loop}.
* ``img.compress`` (lane light) — iLoveIMG "Compress IMAGE": each JPG/PNG/GIF/SVG/WebP is made smaller
  in its own format (``level`` low | recommended | extreme; PNG via pngquant unless ``png_quantize`` is
  off, GIF via gifsicle keeping animations, SVG via scour which never fetches URLs). A file that can't
  get smaller is returned as it was. Output ``<stem>_compress.<ext>``; ``data.files`` lists
  {name, before, after, saved} per input.

Router (mounted under ``/api``):

* ``GET /api/assets/catalog`` — filters, frames, fonts, shapes, stickers (+ credit line), meme canvases,
  watermark positions and the optimisers this server has.
* ``GET /api/assets/stickers/{group}/{name}.svg`` — a bundled sticker (only manifest ids; served with a
  sandboxing CSP).
* ``GET /api/assets/fonts/{font}.ttf`` — a bundled font, so the canvas preview can use the same face.
"""
from __future__ import annotations

import math
import re
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path as PathParam
from fastapi.responses import FileResponse, JSONResponse, Response
from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field

from app.core import registry
from app.imaging import animated, compress, editor, io as imgio, meme, ops as imops, text, watermark
from app.imaging.opreg import Color

MAX_GIF_FRAMES = 300
MAKE_GIF_MAX_PIXELS = 150_000_000   # all frames together (RGBA in memory ≈ 600 MB at the limit)
MAX_GIF_SIDE = 4096
MAX_COMPRESS_FILES = 100
ASSET_MAX_AGE = 86_400
_STICKER_PART = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
_SVG_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": f"public, max-age={ASSET_MAX_AGE}",
    "Cross-Origin-Resource-Policy": "same-origin",
}

router = APIRouter(prefix="/assets", tags=["heyp-assets"])


def _ui(**hints: Any) -> dict[str, Any]:
    return {"ui": hints}


class _Options(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


# ------------------------------------------------------------------ img.make_gif
class MakeGifOptions(_Options):
    delay_ms: int = Field(default=500, ge=20, le=120_000, description="How long each frame shows",
                          json_schema_extra=_ui(widget="number", unit="ms", step=50))
    loop: bool = Field(default=True, description="Loop forever (off: play once)")
    size: int = Field(default=0, ge=0, le=MAX_GIF_SIDE, description="Longest side in px; 0 keeps the first frame's size",
                      json_schema_extra=_ui(widget="number", unit="px", step=10))
    format: Literal["gif", "webp"] = Field(default="gif", json_schema_extra=_ui(widget="segmented"))
    order: list[Annotated[int, Field(ge=0, lt=MAX_GIF_FRAMES)]] | None = Field(
        default=None, max_length=animated.MAX_FRAMES,
        description="Frame order as indices into the job's files (may repeat a file); default: upload order")
    fit: Literal["contain", "cover"] = Field(default="contain", description="Frames of another shape: letterbox or crop")
    background: Color = Field(default="#ffffff", description="Letterbox colour ('transparent' allowed)")
    quality: int = Field(default=85, ge=1, le=100, description="WebP quality")


def _fit_frame(img: Image.Image, size: tuple[int, int], fit: str, background: tuple[int, int, int, int]) -> Image.Image:
    """``img`` fitted onto a ``size`` canvas (RGBA)."""
    rgba = img.convert("RGBA")
    if rgba.size == size:
        return rgba
    if fit == "cover":
        return ImageOps.fit(rgba, size, Image.Resampling.LANCZOS)
    scale = min(size[0] / rgba.width, size[1] / rgba.height)
    inner = (max(1, round(rgba.width * scale)), max(1, round(rgba.height * scale)))
    resized = rgba.resize(inner, Image.Resampling.LANCZOS, reducing_gap=3.0)
    canvas = Image.new("RGBA", size, background)
    canvas.alpha_composite(resized, ((size[0] - inner[0]) // 2, (size[1] - inner[1]) // 2))
    return canvas


def _canvas_size(first: tuple[int, int], size: int, frames: int, budget: int) -> tuple[tuple[int, int], bool]:
    """Canvas for the animation: ``size`` long side (0 = the first frame's), shrunk so frames × pixels
    stays within ``budget``. Returns (size, shrunk)."""
    w, h = first
    scale = size / max(w, h) if size else 1.0
    scale = min(scale, MAX_GIF_SIDE / max(w, h))
    shrunk = False
    if frames * (w * scale) * (h * scale) > budget:
        scale = math.sqrt(budget / (frames * w * h))
        shrunk = True
    return (max(1, int(w * scale)), max(1, int(h * scale))), shrunk


def _decode(item: Any) -> Image.Image:
    """First frame of an input, EXIF-oriented (SVG/PSD/RAW included)."""
    return imgio.load(str(item.path), name=item.name).image


def make_gif(ctx: Any, opts: MakeGifOptions) -> None:
    inputs = list(ctx.inputs)
    order = list(range(len(inputs))) if opts.order is None else list(opts.order)
    if not order:
        raise ValueError("order lists no frames")
    if any(i >= len(inputs) for i in order):
        raise ValueError(f"order refers to file #{max(order)}, but the job has {len(inputs)} file(s)")
    budget = min(MAKE_GIF_MAX_PIXELS, animated.max_anim_pixels(),
                 int(getattr(ctx.settings, "max_anim_pixels", 0) or MAKE_GIF_MAX_PIXELS))
    background = imops.parse_color(opts.background)
    first = _decode(inputs[order[0]])
    canvas, shrunk = _canvas_size(first.size, opts.size, len(order), budget)
    if shrunk:
        ctx.warn(f"The animation was made {canvas[0]}×{canvas[1]} px so that {len(order)} frames fit the "
                 "server's animation size limit")
    frames: list[Image.Image] = []
    cache: dict[int, Image.Image] = {order[0]: _fit_frame(first, canvas, opts.fit, background)}
    del first
    for n, index in enumerate(order):
        ctx.check_cancel()
        frame = cache.get(index)
        if frame is None:  # decoded straight from disk: hundreds of photos must not churn the store's LRU
            frame = cache[index] = _fit_frame(_decode(inputs[index]), canvas, opts.fit, background)
        frames.append(frame)
        ctx.progress(0.7 * (n + 1) / len(order), f"Frame {n + 1} of {len(order)}")
    if not any(imgio.has_alpha(f) for f in cache.values()):
        frames = [f.convert("RGB") for f in frames]
    ctx.progress(0.75, "Encoding the animation")
    data = animated.encode(frames, [opts.delay_ms] * len(frames), 0 if opts.loop else None, opts.format,
                           quality=opts.quality, background=opts.background)
    ctx.check_cancel()
    stem = inputs[order[0]].stem
    ctx.add_output(data, ctx.output_name(stem, opts.format), imgio.mime_for(opts.format), kind="image")
    ctx.data.update({"frames": len(frames), "width": canvas[0], "height": canvas[1], "format": opts.format,
                     "delay_ms": opts.delay_ms, "loop": opts.loop})
    ctx.progress(1.0, "Done")


# ------------------------------------------------------------------ img.compress
class CompressOptions(_Options):
    level: Literal["low", "recommended", "extreme"] = Field(
        default="recommended", description="low keeps the most detail, extreme makes the smallest files",
        json_schema_extra=_ui(widget="segmented"))
    png_quantize: bool = Field(default=True, description="Reduce PNG colours (lossy, much smaller); off = lossless only")


def _input_format(item: Any) -> str | None:
    fmt = (item.info or {}).get("format") if hasattr(item, "info") else None
    if fmt:
        try:
            return imgio.canonical_format(str(fmt))
        except ValueError:
            return None
    try:
        with open(item.path, "rb") as fh:
            head = fh.read(64 * 1024)
    except OSError:
        return None
    kind = imgio.sniff(head)
    if kind:
        return kind
    try:
        with Image.open(item.path) as im:
            return imgio.canonical_format(im.format or "")
    except Exception:
        return None


def compress_files(ctx: Any, opts: CompressOptions) -> None:
    rows: list[dict[str, Any]] = []
    total_in = total_out = 0
    for n, item in enumerate(ctx.inputs):
        ctx.check_cancel()
        fmt = _input_format(item)
        if fmt not in compress.OPTIMIZABLE:
            ctx.warn(f"{item.name}: only JPG, PNG, GIF, SVG and WebP files can be compressed — skipped")
            continue
        data = item.path.read_bytes()
        try:
            result = compress.optimize_file(data, fmt, opts.level, png_quantize=opts.png_quantize)
        except compress.ToolUnavailable as exc:
            ctx.warn(f"{item.name}: {exc}")
            continue
        if result.note and result.changed:
            ctx.warn(f"{item.name}: {result.note}")
        ext = imgio.ext_for(fmt)
        info = ctx.add_output(result.data, ctx.output_name(item.stem, ext), imgio.mime_for(fmt), kind="image")
        before, after = len(data), len(result.data)
        total_in, total_out = total_in + before, total_out + after
        rows.append({"id": info.get("id"), "name": info.get("name"), "source": item.name, "before": before,
                     "after": after, "saved": round(1 - after / before, 4) if before else 0.0,
                     "changed": result.changed})
        ctx.progress((n + 1) / len(ctx.inputs), f"{n + 1} of {len(ctx.inputs)} files")
    if not rows:
        raise ValueError("None of these files can be compressed: use JPG, PNG, GIF, SVG or WebP")
    ctx.data.update({"files": rows, "before": total_in, "after": total_out,
                     "saved": round(1 - total_out / total_in, 4) if total_in else 0.0})


# ------------------------------------------------------------------ registration
MAKE_GIF = registry.register(registry.Tool(
    id="img.make_gif", title="Make GIF", category="Create",
    summary="Turn photos into an animated GIF or WebP, one frame per photo.",
    inputs=("image",), options=MakeGifOptions, run=make_gif, min_files=1, max_files=MAX_GIF_FRAMES,
    lane="heavy", timeout_s=300, limits={"max_files": MAX_GIF_FRAMES, "memory_mb": 1536},
    outputs="One animated GIF or WebP named <first photo>_make_gif.gif"))
COMPRESS = registry.register(registry.Tool(
    id="img.compress", title="Compress images", category="Optimize",
    summary="Make JPG, PNG, GIF, SVG and WebP files smaller in their own format.",
    inputs=("image",), options=CompressOptions, run=compress_files, min_files=1, max_files=MAX_COMPRESS_FILES,
    lane="light", timeout_s=600, limits={"max_files": MAX_COMPRESS_FILES},
    outputs="One file per input, same format, named <stem>_compress.<ext>"))


# ------------------------------------------------------------------ assets router
def catalog() -> dict[str, Any]:
    """Everything the image editors list (static per server; cached by the browser)."""
    data = editor.catalog()
    data.update({
        "positions": list(watermark.POSITIONS) + ["mosaic"],
        "canvases": [{"id": k, "width": w, "height": h} for k, (w, h) in meme.CANVAS_PRESETS.items()],
        "optimizers": {name: compress.tool_available(name) for name in ("gifsicle", "pngquant", "scour")},
    })
    return data


@router.get("/catalog")
def get_catalog() -> JSONResponse:
    return JSONResponse(catalog(), headers={"Cache-Control": "public, max-age=3600"})


@router.get("/stickers/{group}/{name}.svg")
def get_sticker(group: Annotated[str, PathParam(max_length=40)],
                name: Annotated[str, PathParam(max_length=90)]) -> Response:
    sticker_id = f"{group}/{name}"
    if not (_STICKER_PART.fullmatch(group) and _STICKER_PART.fullmatch(name)) or not editor.has_sticker(sticker_id):
        raise HTTPException(status_code=404, detail="Unknown sticker")
    return Response(editor.sticker_svg(sticker_id).encode("utf-8"), media_type="image/svg+xml", headers=_SVG_HEADERS)


@router.get("/fonts/{font}.ttf")
def get_font(font: Annotated[str, PathParam(max_length=40)]) -> FileResponse:
    try:
        path = text.font_path(font)
    except (ValueError, RuntimeError):
        raise HTTPException(status_code=404, detail="Unknown font") from None
    return FileResponse(path, media_type="font/ttf",
                        headers={"Cache-Control": f"public, max-age={ASSET_MAX_AGE}", "X-Content-Type-Options": "nosniff"})


__all__ = ["router", "MakeGifOptions", "CompressOptions", "make_gif", "compress_files", "catalog"]
