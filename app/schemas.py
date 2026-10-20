"""Pydantic v2 request models (PLAN.md §3). Ops form a discriminated union on ``"op"``.

The union is built from the image-op registry (``app.imaging.opreg``, PLAN.md §3 "Ops"): every
plug-in in ``app/imaging/ops_ext/`` is imported first, then ``Op`` covers all registered op models plus
the v1 models below for any op no plug-in has taken over yet (the v1 compatibility shim — the pipeline
registers their v1 handlers as fallbacks). The v1 JSON is unchanged.
"""
from __future__ import annotations

import math
import re
from typing import Annotated, Literal, Union

from pydantic import AfterValidator, BaseModel, Field, TypeAdapter, field_validator, model_validator

from app.imaging import opreg
from app.imaging.opreg import Color, OpModel, _check_color  # noqa: F401  (re-exported for v1 imports)
from app.store import ID_RE, clean_name

MAX_DIMENSION = 30_000
MAX_OPS = 32
MAX_IDS = 10_000  # structural bound only; the endpoints enforce HEYP_MAX_FILES from the app's settings
MIN_PHOTO_MM = 5  # smaller sheet photos are not printable (and would tile millions of copies)
INPUT_FORMATS = ["heic", "heif", "jpeg", "png", "webp", "avif", "tiff", "bmp", "gif", "ico", "jp2"]
OUTPUT_FORMATS = ["jpeg", "png", "webp", "avif", "heic", "tiff", "bmp", "gif", "pdf"]
_UNIT_PER_INCH = {"in": 1.0, "cm": 2.54, "mm": 25.4}
_EXT_RE = re.compile(r"\.(jpe?g|png|webp|avif|heic|heif|tiff?|bmp|gif|pdf|zip)$", re.IGNORECASE)


def ratio_value(ratio: str | float) -> float:
    """Parse "16:9", "4/5", "1.5" or 1.5 into a positive float (ValueError if invalid)."""
    if isinstance(ratio, (int, float)) and not isinstance(ratio, bool):
        value = float(ratio)
    else:
        text = str(ratio).strip()
        parts = re.split(r"\s*[:/x×]\s*", text)
        try:
            if len(parts) == 2:
                num, den = float(parts[0]), float(parts[1])
                value = num / den if den else math.inf
            elif len(parts) == 1:
                value = float(parts[0])
            else:
                raise ValueError
        except ValueError:
            raise ValueError(f"invalid ratio {ratio!r} (use 'W:H', 'W/H' or a number)") from None
    if not math.isfinite(value) or not 0.01 <= value <= 100:
        raise ValueError(f"ratio {ratio!r} must be between 1:100 and 100:1")
    return value


def _check_ratio(value: str | float) -> str | float:
    ratio_value(value)
    return value


_Model = OpModel  # extra="forbid", allow_inf_nan=False


# ------------------------------------------------------------------ ops
class RotateOp(_Model):
    op: Literal["rotate"]
    angle: float = Field(ge=-360, le=360)
    expand: bool = True
    fill: Color = "#ffffff"


class FlipOp(_Model):
    op: Literal["flip"]
    direction: Literal["horizontal", "vertical"]


class CropOp(_Model):
    op: Literal["crop"]
    x: float = Field(ge=0, le=1_000_000)
    y: float = Field(ge=0, le=1_000_000)
    width: float = Field(gt=0, le=1_000_000)
    height: float = Field(gt=0, le=1_000_000)
    unit: Literal["px", "fraction"] = "px"

    @model_validator(mode="after")
    def _fractions(self) -> CropOp:
        if self.unit == "fraction":
            for name in ("x", "y", "width", "height"):
                if getattr(self, name) > 1:
                    raise ValueError(f"crop {name} must be within [0, 1] when unit is 'fraction'")
        return self


class CropAspectOp(_Model):
    op: Literal["crop_aspect"]
    ratio: Annotated[Union[float, str], AfterValidator(_check_ratio)]
    anchor: Literal["center", "smart", "face"] = "center"


class TrimOp(_Model):
    op: Literal["trim"]
    tolerance: int = Field(default=12, ge=0, le=255)
    padding: int = Field(default=0, ge=0, le=10_000)


ResizeMode = Literal["fit", "fill", "pad", "exact", "scale", "long_edge", "short_edge", "megapixels"]


class ResizeOp(_Model):
    op: Literal["resize"]
    mode: ResizeMode
    width: float | None = Field(default=None, gt=0, le=MAX_DIMENSION)
    height: float | None = Field(default=None, gt=0, le=MAX_DIMENSION)
    scale: float | None = Field(default=None, ge=1, le=1000)
    size: float | None = Field(default=None, gt=0, le=MAX_DIMENSION)
    megapixels: float | None = Field(default=None, gt=0, le=1000)
    unit: Literal["px", "in", "cm", "mm"] = "px"
    dpi: int = Field(default=300, ge=1, le=2400)
    upscale: bool = True
    resample: Literal["lanczos", "bicubic", "bilinear", "nearest"] = "lanczos"
    background: Color = "#ffffff"

    @model_validator(mode="after")
    def _combo(self) -> ResizeOp:
        mode = self.mode
        if mode == "fit" and self.width is None and self.height is None:
            raise ValueError("resize mode 'fit' needs width and/or height")
        if mode in ("fill", "pad", "exact") and (self.width is None or self.height is None):
            raise ValueError(f"resize mode {mode!r} needs both width and height")
        if mode == "scale" and self.scale is None:
            raise ValueError("resize mode 'scale' needs scale (percent)")
        if mode in ("long_edge", "short_edge") and self.size is None:
            raise ValueError(f"resize mode {mode!r} needs size")
        if mode == "megapixels" and self.megapixels is None:
            raise ValueError("resize mode 'megapixels' needs megapixels")
        for name in ("width", "height", "size"):
            value = getattr(self, name)
            if value is None:
                continue
            px = value if self.unit == "px" else value / _UNIT_PER_INCH[self.unit] * self.dpi
            if px < 1 and self.unit == "px":
                raise ValueError(f"resize {name} must be at least 1 px")
            if px > MAX_DIMENSION:
                raise ValueError(f"resize {name} is {round(px)} px at {self.dpi} dpi (max {MAX_DIMENSION})")
        return self


class PadOp(_Model):
    op: Literal["pad"]
    top: int = Field(default=0, ge=0, le=10_000)
    right: int = Field(default=0, ge=0, le=10_000)
    bottom: int = Field(default=0, ge=0, le=10_000)
    left: int = Field(default=0, ge=0, le=10_000)
    color: Color = "#ffffff"


class AdjustOp(_Model):
    op: Literal["adjust"]
    brightness: float = Field(default=1.0, ge=0, le=10)
    contrast: float = Field(default=1.0, ge=0, le=10)
    saturation: float = Field(default=1.0, ge=0, le=10)
    sharpness: float = Field(default=1.0, ge=0, le=10)
    grayscale: bool = False
    auto_contrast: bool = False


class BackgroundOp(_Model):
    op: Literal["background"]
    mode: Literal["remove", "replace"]
    color: Color = "#ffffff"


class CustomPreset(_Model):
    """User-defined passport geometry (preset ``"custom"``)."""

    width_mm: float = Field(gt=0, le=500)
    height_mm: float = Field(gt=0, le=500)
    dpi: int = Field(default=300, ge=1, le=2400)
    pixel_width: int | None = Field(default=None, ge=1, le=MAX_DIMENSION)
    pixel_height: int | None = Field(default=None, ge=1, le=MAX_DIMENSION)
    head_min: float = Field(default=0.5, gt=0, lt=1)
    head_max: float = Field(default=0.69, gt=0, lt=1)
    eye_min: float | None = Field(default=None, gt=0, lt=1)
    eye_max: float | None = Field(default=None, gt=0, lt=1)
    background: Color = "#ffffff"

    @model_validator(mode="after")
    def _ranges(self) -> CustomPreset:
        if self.head_min > self.head_max:
            raise ValueError("head_min must be <= head_max")
        if (self.eye_min is None) != (self.eye_max is None):
            raise ValueError("eye_min and eye_max must be given together")
        if self.eye_min is not None and self.eye_max is not None and self.eye_min > self.eye_max:
            raise ValueError("eye_min must be <= eye_max")
        if (self.pixel_width is None) != (self.pixel_height is None):
            raise ValueError("pixel_width and pixel_height must be given together")
        for name, mm in (("width_mm", self.width_mm), ("height_mm", self.height_mm)):
            if self.pixel_width is None and round(mm / 25.4 * self.dpi) > MAX_DIMENSION:
                raise ValueError(f"{name} at {self.dpi} dpi exceeds {MAX_DIMENSION} px")
            if self.pixel_width is None and round(mm / 25.4 * self.dpi) < 1:
                raise ValueError(f"{name} at {self.dpi} dpi is smaller than 1 px")
        return self


class PassportOp(_Model):
    op: Literal["passport"]
    preset: str = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    custom: CustomPreset | None = None
    background: Color | None = None
    replace_background: bool = True
    face_index: int = Field(default=0, ge=0, le=100)
    zoom: float = Field(default=1.0, ge=0.5, le=2.0)
    offset_x: float = Field(default=0.0, ge=-1, le=1)
    offset_y: float = Field(default=0.0, ge=-1, le=1)
    level: bool = True  # auto-level head roll measured from the eye landmarks

    @model_validator(mode="after")
    def _custom(self) -> PassportOp:
        if self.preset == "custom" and self.custom is None:
            raise ValueError("preset 'custom' requires the 'custom' object")
        return self


class SheetOp(_Model):
    op: Literal["sheet"]
    paper: str = Field(default="4x6", min_length=1, max_length=32, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    orientation: Literal["auto", "portrait", "landscape"] = "auto"
    photo_width_mm: float | None = Field(default=None, ge=MIN_PHOTO_MM, le=500)
    photo_height_mm: float | None = Field(default=None, ge=MIN_PHOTO_MM, le=500)
    dpi: int | None = Field(default=None, ge=72, le=1200)  # None → pipeline dpi (300–600), else 300
    gap_mm: float = Field(default=2, ge=0, le=50)
    margin_mm: float = Field(default=4, ge=0, le=50)
    cut_lines: bool = True
    copies: int = Field(default=0, ge=0, le=200)


V1_OPS: tuple[type[BaseModel], ...] = (RotateOp, FlipOp, CropOp, CropAspectOp, TrimOp, ResizeOp, PadOp,
                                        AdjustOp, BackgroundOp, PassportOp, SheetOp)


# ------------------------------------------------------------------ output / request
class OutputSpec(_Model):
    format: Literal["jpeg", "png", "webp", "avif", "heic", "tiff", "bmp", "gif", "pdf", "original"] = "jpeg"
    quality: int = Field(default=90, ge=1, le=100)
    lossless: bool = False
    progressive: bool = True
    chroma: Literal["auto", "444", "422", "420"] = "auto"
    dpi: int | None = Field(default=None, ge=1, le=2400)
    strip_metadata: bool = True
    strip_gps: bool = True
    colorspace: Literal["srgb", "keep"] = "srgb"
    background: Color = "#ffffff"
    max_kb: float | None = Field(default=None, gt=0, le=100_000)
    min_kb: float | None = Field(default=None, gt=0, le=100_000)
    combine_pdf: bool = True
    filename: str | None = Field(default=None, max_length=200)

    @field_validator("format", mode="before")
    @classmethod
    def _format_alias(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip().lower()
            return {"jpg": "jpeg", "tif": "tiff", "heif": "heic"}.get(value, value)
        return value

    @field_validator("filename")
    @classmethod
    def _filename(cls, value: str | None) -> str | None:
        if value is None:
            return None
        name = _EXT_RE.sub("", clean_name(value, default="", max_len=120))
        return name.strip(" .") or None

    @model_validator(mode="after")
    def _sizes(self) -> OutputSpec:
        if self.max_kb is not None and self.min_kb is not None and self.min_kb > self.max_kb:
            raise ValueError("min_kb must be <= max_kb")
        return self


def _check_id(value: str) -> str:
    if not ID_RE.fullmatch(value):
        raise ValueError("invalid file id")
    return value


FileId = Annotated[str, Field(max_length=32), AfterValidator(_check_id)]


# ------------------------------------------------------------------ the Op union (from the registry)
def _op_name(model: type[BaseModel]) -> str:
    name = opreg._literal_name(model)
    assert name is not None, model
    return name


def _build_union() -> tuple[type[BaseModel], ...]:
    """Registered op models (v1 order first, then plug-ins by name) plus unclaimed v1 models."""
    registered = {spec.name: spec.model for spec in opreg.all_ops()}
    models = [registered.get(_op_name(m), m) for m in V1_OPS]
    v1_names = {_op_name(m) for m in V1_OPS}
    models += [registered[n] for n in sorted(registered) if n not in v1_names]
    return tuple(models)


opreg.load_plugins()  # every ops_ext module registers before the union exists
OP_MODELS: tuple[type[BaseModel], ...] = _build_union()
OP_NAMES: tuple[str, ...] = tuple(_op_name(m) for m in OP_MODELS)
Op = Annotated[Union[OP_MODELS], Field(discriminator="op")]  # type: ignore[valid-type]
OPS_ADAPTER: TypeAdapter[list[Op]] = TypeAdapter(list[Op])
# A plug-in may replace a v1 model (e.g. rotate gains a field): app.schemas names follow the registry.
(RotateOp, FlipOp, CropOp, CropAspectOp, TrimOp, ResizeOp, PadOp, AdjustOp, BackgroundOp, PassportOp,
 SheetOp) = OP_MODELS[:len(V1_OPS)]

OpList = Annotated[list[Op], Field(max_length=MAX_OPS)]


class ProcessRequest(_Model):
    ids: list[FileId] = Field(min_length=1, max_length=MAX_IDS)
    ops: OpList = Field(default_factory=list)
    ops_by_id: dict[str, OpList] | None = None  # per-file ops; replace ``ops`` for those ids
    output: OutputSpec = Field(default_factory=OutputSpec)

    @field_validator("ids")
    @classmethod
    def _unique_ids(cls, value: list[str]) -> list[str]:
        """Repeated ids would only redo identical work, so each file is processed once (first position)."""
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def _ops_by_id_keys(self) -> ProcessRequest:
        if self.ops_by_id:
            unknown = set(self.ops_by_id) - set(self.ids)
            if unknown:
                raise ValueError(f"ops_by_id has {len(unknown)} id(s) that are not in ids")
        return self

    def ops_for(self, file_id: str) -> list[Op]:
        """The op list for one file: its ``ops_by_id`` entry when present, else the shared ``ops``."""
        if self.ops_by_id and file_id in self.ops_by_id:
            return self.ops_by_id[file_id]
        return self.ops
