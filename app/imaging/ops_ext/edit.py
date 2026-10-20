"""Watermark, meme and photo-editor ops (PLAN.md §3 "Ops").

Every op also runs per frame of an animated GIF/WebP: its layers are rendered once per animation and
stamped on each frame. All sizes are fractions, so an edit planned on a preview lands identically on
the full-size image. JSON (fields other than ``op`` are optional unless noted):

* ``{"op": "watermark", "kind": "text"|"image", "text", "font": "libre-franklin", "size": 0.05,
  "color": "#ffffff", "bold": false, "shadow": null, "opacity": 0.6, "rotation": 0,
  "position": "bottom-right"|<9-grid>|"mosaic"|"custom", "x", "y", "margin": 0.03, "spacing": 0.5,
  "image_id": <file id>, "image_scale": 0.25}`` — text ``size`` and ``margin`` are fractions of the
  shorter side, ``image_scale`` of the width, ``x``/``y`` the centre for ``custom``; ``spacing`` is the
  mosaic gap as a fraction of the mark. ``text`` is required for text marks, ``image_id`` (another
  upload) for image marks.
* ``{"op": "meme", "top", "bottom", "font": "anton", "size": "auto"|<fraction of the height>,
  "color", "outline": "#000000"|"none", "outline_width": 0.07, "uppercase": true,
  "layout": "inside"|"outside", "band_color": "#ffffff"}`` — at least one caption.
* ``{"op": "filter", "name": <one of FILTERS>, "intensity": 1.0}``.
* ``{"op": "frame", "style": <one of FRAMES>, "color": null, "width": null, "background": null}`` —
  ``width`` is a fraction of the shorter side (style default when null); most styles enlarge the canvas.
* ``{"op": "overlay", "items": [...]}`` — items in drawing order (see ``app.imaging.editor``):
  ``{"type": "text", "text", "x", "y", "size", "font", "color", "align", "rotation", "opacity"}``,
  ``{"type": "sticker", "sticker": "openmoji/grinning-face", "x", "y", "w", "h", "rotation", "opacity"}``,
  ``{"type": "shape", "shape": "rect"|"ellipse"|"line"|"arrow"|"star"|"heart", "x", "y", "w", "h",
  "color", "fill", "stroke_width", "rotation", "opacity"}``,
  ``{"type": "brush", "points": [[x, y], …], "color", "width", "opacity"}``.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, Literal, Union

from PIL import Image
from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator

from app.imaging import editor, meme, text, watermark
from app.imaging.opreg import Color, Ctx, OpModel, OpSpec, check_pixels, register_op
from app.schemas import FileId

MAX_TEXT = 500
MAX_CAPTION = 300


# ------------------------------------------------------------------ shared field types
def _check_font(value: str) -> str:
    text.resolve(value)  # ValueError (→ 422) for unknown fonts; every spelling of an id is accepted
    return value


def _check_sticker(value: str) -> str:
    if not editor.has_sticker(value):
        raise ValueError(f"unknown sticker {value!r}")
    return value


FontId = Annotated[str, Field(min_length=1, max_length=40), AfterValidator(_check_font)]
StickerId = Annotated[str, Field(min_length=3, max_length=100), AfterValidator(_check_sticker)]
Coord = Annotated[float, Field(ge=-1, le=2)]       # centre positions may hang off the picture
Rotation = Annotated[float, Field(ge=-360, le=360)]
Opacity = Annotated[float, Field(ge=0, le=1)]
POSITIONS = Literal["top-left", "top", "top-right", "left", "center", "right", "bottom-left", "bottom",
                    "bottom-right", "mosaic", "custom"]
FilterName = Literal[tuple(["none", *editor.FILTERS])]  # type: ignore[valid-type]
FrameStyle = Literal[tuple(["none", *editor.FRAMES])]   # type: ignore[valid-type]


def _meta_key(op: BaseModel, kind: str) -> str:
    """Per-op scratch key in ``ctx.meta`` (one op object = one entry of the request's op list)."""
    return f"_{kind}:{id(op)}"


def _warn_text(ctx: Ctx, strings: Sequence[str | None], font: str, extra: Sequence[str] = (), *,
               bold: bool = False) -> None:
    """Warn (once per request) about text the server can't draw faithfully: characters missing from every
    bundled font, complex scripts without raqm shaping (img-02), and op-specific notes in ``extra``."""
    for message in [*extra, *text.text_warnings(strings, font, bold=bold)]:
        ctx.warn(message)


def _per_size(ctx: Ctx, op: BaseModel, kind: str, size: tuple[int, int], build: Any) -> Any:
    """``build()`` once per op and canvas size (every animation frame reuses it)."""
    key = _meta_key(op, kind)
    cached = ctx.meta.get(key)
    if cached is not None and cached[0] == size:
        return cached[1]
    value = build()
    ctx.meta[key] = (size, value)
    return value


# ------------------------------------------------------------------ watermark
class WatermarkOp(OpModel):
    op: Literal["watermark"]
    kind: Literal["text", "image"] = "text"
    text: str = Field(default="", max_length=MAX_TEXT)
    font: FontId = "libre-franklin"
    size: float = Field(default=0.05, gt=0, le=0.5)
    color: Color = "#ffffff"
    bold: bool = False
    shadow: Color | None = None
    opacity: Opacity = 0.6
    rotation: Rotation = 0.0
    position: POSITIONS = "bottom-right"
    x: float | None = Field(default=None, ge=0, le=1)
    y: float | None = Field(default=None, ge=0, le=1)
    margin: float = Field(default=0.03, ge=0, le=0.45)
    spacing: float = Field(default=0.5, ge=0, le=5)
    image_id: FileId | None = None
    image_scale: float = Field(default=0.25, gt=0, le=1)

    @field_validator("position", mode="before")
    @classmethod
    def _position_alias(cls, value: object) -> object:
        if isinstance(value, str):
            key = value.strip().lower().replace("_", "-").replace(" ", "-")
            return watermark._ALIASES.get(key, key)  # noqa: SLF001
        return value

    @model_validator(mode="after")
    def _content(self) -> WatermarkOp:
        if self.kind == "text" and not self.text.strip():
            raise ValueError("watermark text is empty")
        if self.kind == "image" and not self.image_id:
            raise ValueError("an image watermark needs image_id (another uploaded image)")
        return self


def _watermark_image(ctx: Ctx, file_id: str) -> Image.Image:
    loaded = ctx.load_file(file_id)
    image = getattr(loaded, "image", None)
    if not isinstance(image, Image.Image):
        raise ValueError("The watermark file is not an image")
    return image


def _watermark_layer(ctx: Ctx, op: WatermarkOp, size: tuple[int, int]) -> Image.Image:
    return _per_size(ctx, op, "watermark", size,
                     lambda: watermark.prepare(op, size, lambda fid: _watermark_image(ctx, fid)))


def _watermark(ctx: Ctx, op: WatermarkOp, _prefix: Sequence[BaseModel]) -> None:
    if op.kind == "text":
        _warn_text(ctx, [op.text], op.font, bold=True)   # text marks are set at weight 600+
    ctx.image = watermark.stamp(ctx.image, op, _watermark_layer(ctx, op, ctx.image.size))


def _watermark_frame(ctx: Ctx, op: WatermarkOp, frame: Image.Image) -> Image.Image:
    if op.kind == "text" and ctx.frame_index == 0:
        _warn_text(ctx, [op.text], op.font, bold=True)
    return watermark.stamp(frame, op, _watermark_layer(ctx, op, frame.size))


# ------------------------------------------------------------------ meme
class MemeOp(OpModel):
    op: Literal["meme"]
    top: str = Field(default="", max_length=MAX_CAPTION)
    bottom: str = Field(default="", max_length=MAX_CAPTION)
    font: FontId = "anton"
    size: Literal["auto"] | Annotated[float, Field(gt=0, le=0.5)] = "auto"
    color: Color | None = None                  # None: white inside, black on outside bands
    outline: Literal["none"] | Color | None = None  # None: black inside, none outside
    outline_width: float = Field(default=meme.DEFAULT_OUTLINE_WIDTH, ge=0, le=0.3)
    uppercase: bool = True
    layout: Literal["inside", "outside"] = "inside"
    band_color: Color = "#ffffff"

    @model_validator(mode="after")
    def _captions(self) -> MemeOp:
        if not self.top.strip() and not self.bottom.strip():
            raise ValueError("enter a top or a bottom caption")
        return self


def _meme_prepared(ctx: Ctx, op: MemeOp, size: tuple[int, int]) -> meme.Prepared:
    return _per_size(ctx, op, "meme", size, lambda: meme.prepare(op, size))


def _meme_check(ctx: Ctx, prepared: meme.Prepared, size: tuple[int, int]) -> None:
    if ctx.max_pixels:
        check_pixels(*prepared.output_size(size), ctx.max_pixels, "meme")


def _meme(ctx: Ctx, op: MemeOp, _prefix: Sequence[BaseModel]) -> None:
    prepared = _meme_prepared(ctx, op, ctx.image.size)
    _meme_check(ctx, prepared, ctx.image.size)
    _warn_text(ctx, [op.top, op.bottom], op.font, prepared.warnings)
    ctx.image = meme.stamp(ctx.image, prepared)


def _meme_frame(ctx: Ctx, op: MemeOp, frame: Image.Image) -> Image.Image:
    prepared = _meme_prepared(ctx, op, frame.size)
    if ctx.frame_index == 0:
        _meme_check(ctx, prepared, frame.size)
        _warn_text(ctx, [op.top, op.bottom], op.font, prepared.warnings)
    return meme.stamp(frame, prepared)


def _meme_plan(size: tuple[float, float], op: MemeOp, dpi: float | None) -> tuple[tuple[float, float], float | None]:
    w, h = size
    if op.layout == "outside":  # two bands of at most 30 % of the height each, plus margins
        return (w, h * 1.7 + 8), dpi
    return (w, h), dpi


# ------------------------------------------------------------------ filter
class FilterOp(OpModel):
    op: Literal["filter"]
    name: FilterName
    intensity: float = Field(default=1.0, ge=0, le=1)

    @field_validator("name", mode="before")
    @classmethod
    def _alias(cls, value: object) -> object:
        if isinstance(value, str):
            key = value.strip().lower()
            return editor.FILTER_ALIASES.get(key, key)
        return value


def _filter(ctx: Ctx, op: FilterOp, _prefix: Sequence[BaseModel]) -> None:
    ctx.image = editor.apply_filter(ctx.image, op.name, op.intensity)


def _filter_frame(ctx: Ctx, op: FilterOp, frame: Image.Image) -> Image.Image:
    return editor.apply_filter(frame, op.name, op.intensity)


# ------------------------------------------------------------------ frame
class FrameOp(OpModel):
    op: Literal["frame"]
    style: FrameStyle
    color: Color | None = None
    width: float | None = Field(default=None, ge=0, le=editor.MAX_FRAME_WIDTH)
    background: Color | None = None

    @field_validator("style", mode="before")
    @classmethod
    def _alias(cls, value: object) -> object:
        if isinstance(value, str):
            key = value.strip().lower()
            return editor.FRAME_ALIASES.get(key, key)
        return value


def _frame_check(ctx: Ctx, op: FrameOp, size: tuple[int, int]) -> None:
    if ctx.max_pixels and op.style != "none":
        check_pixels(*editor.frame_size(op.style, size, op.width), ctx.max_pixels, "frame")


def _frame(ctx: Ctx, op: FrameOp, _prefix: Sequence[BaseModel]) -> None:
    _frame_check(ctx, op, ctx.image.size)
    ctx.image = editor.apply_frame(ctx.image, op.style, op.color, op.width, op.background)


def _frame_frame(ctx: Ctx, op: FrameOp, frame: Image.Image) -> Image.Image:
    if ctx.frame_index == 0:
        _frame_check(ctx, op, frame.size)
    return editor.apply_frame(frame, op.style, op.color, op.width, op.background)


def _frame_plan(size: tuple[float, float], op: FrameOp, dpi: float | None) -> tuple[tuple[float, float], float | None]:
    if op.style == "none":
        return size, dpi
    return editor.frame_size(op.style, size, op.width), dpi


# ------------------------------------------------------------------ overlay
class _Item(OpModel):
    x: Coord = 0.5
    y: Coord = 0.5
    rotation: Rotation = 0.0
    opacity: Opacity = 1.0


class TextItem(_Item):
    type: Literal["text"]
    text: str = Field(max_length=MAX_TEXT)
    size: float = Field(default=0.08, gt=0, le=1)          # font px / image height
    font: FontId = "libre-franklin"
    color: Color = "#ffffff"
    align: Literal["left", "center", "right"] = "center"
    bold: bool = False
    outline: Color | None = None
    outline_width: float = Field(default=0.0, ge=0, le=0.3)  # × font size
    background: Color | None = None
    max_width: float | None = Field(default=None, gt=0, le=3)  # wrap width / image width


class StickerItem(_Item):
    type: Literal["sticker"]
    sticker: StickerId
    w: float = Field(default=0.2, gt=0, le=3)              # / image width
    h: float | None = Field(default=None, gt=0, le=3)       # / image height (fit inside w × h)
    flip: bool = False


class ShapeItem(_Item):
    type: Literal["shape"]
    shape: Literal["rect", "ellipse", "line", "arrow", "star", "heart"]
    w: float = Field(default=0.3, gt=0, le=3)              # / image width
    h: float = Field(default=0.2, gt=0, le=3)              # / image height
    color: Color = "#e0262e"
    fill: Color | None = None
    stroke_width: float = Field(default=0.008, ge=0, le=0.25)  # / image width


Point = tuple[Coord, Coord]


class BrushItem(OpModel):
    type: Literal["brush"]
    points: list[Point] = Field(min_length=1, max_length=editor.MAX_BRUSH_POINTS)
    color: Color = "#e0262e"
    width: float = Field(default=0.012, gt=0, le=0.25)      # / image width
    opacity: Opacity = 1.0
    mode: Literal["pen", "highlighter"] = "pen"


OverlayItem = Annotated[Union[TextItem, StickerItem, ShapeItem, BrushItem], Field(discriminator="type")]


class OverlayOp(OpModel):
    op: Literal["overlay"]
    items: list[OverlayItem] = Field(default_factory=list, max_length=editor.MAX_ITEMS)

    @model_validator(mode="after")
    def _budget(self) -> OverlayOp:
        points = sum(len(i.points) for i in self.items if isinstance(i, BrushItem))
        if points > editor.MAX_BRUSH_POINTS:
            raise ValueError(f"the drawing has {points} points (max {editor.MAX_BRUSH_POINTS}); simplify it")
        return self


def _overlay_layers(ctx: Ctx, op: OverlayOp, size: tuple[int, int]) -> list[editor.Placed]:
    return _per_size(ctx, op, "overlay", size,
                     lambda: editor.prepare_overlay(op.items, size, check_cancel=ctx.check_cancel))


def _overlay_text_warnings(ctx: Ctx, op: OverlayOp) -> None:
    for item in op.items:
        if isinstance(item, TextItem):
            _warn_text(ctx, [item.text], item.font, bold=item.bold)


def _overlay(ctx: Ctx, op: OverlayOp, _prefix: Sequence[BaseModel]) -> None:
    _overlay_text_warnings(ctx, op)
    if op.items:
        ctx.image = editor.composite_overlay(ctx.image, _overlay_layers(ctx, op, ctx.image.size))


def _overlay_frame(ctx: Ctx, op: OverlayOp, frame: Image.Image) -> Image.Image:
    if ctx.frame_index == 0:
        _overlay_text_warnings(ctx, op)
    if not op.items:
        return frame
    return editor.composite_overlay(frame, _overlay_layers(ctx, op, frame.size))


# ------------------------------------------------------------------ registration
WATERMARK = register_op(OpSpec("watermark", WatermarkOp, _watermark, frame_apply=_watermark_frame, cost=0.03,
                               fixed_cost=0.05, extra_bytes=8, label="Watermark"))
MEME = register_op(OpSpec("meme", MemeOp, _meme, frame_apply=_meme_frame, geometry=True, cost=0.02,
                          fixed_cost=0.1, extra_bytes=8, plan_size=_meme_plan, label="Meme"))
FILTER = register_op(OpSpec("filter", FilterOp, _filter, frame_apply=_filter_frame, cost=0.06, extra_bytes=10,
                            label="Filter"))
FRAME = register_op(OpSpec("frame", FrameOp, _frame, frame_apply=_frame_frame, geometry=True, cost=0.04,
                           extra_bytes=8, plan_size=_frame_plan, label="Frame"))
OVERLAY = register_op(OpSpec("overlay", OverlayOp, _overlay, frame_apply=_overlay_frame, cost=0.03,
                             fixed_cost=0.1, extra_bytes=8, label="Photo editor layers"))
