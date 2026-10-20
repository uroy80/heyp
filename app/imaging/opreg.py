"""Image-op plug-in registry for the heyp editing pipeline (PLAN.md §3 "Ops").

An op is a pydantic model with ``op: Literal["<name>"]`` plus an :class:`OpSpec` registered here.
``app.schemas`` builds the request ``Op`` union from the registry and ``pipeline.run`` dispatches
through it, so a new op is one module in ``app/imaging/ops_ext/`` (auto-imported)::

    from typing import Literal
    from app.imaging.opreg import Color, Ctx, OpModel, OpSpec, register_op

    class SepiaOp(OpModel):
        op: Literal["sepia"]
        strength: float = Field(default=1.0, ge=0, le=1)

    def apply(ctx: Ctx, op: SepiaOp, prefix) -> None:      # whole (still) image
        ctx.image = make_sepia(ctx.image, op.strength)

    def frame(ctx: Ctx, op: SepiaOp, image):              # one animation frame → new frame
        return make_sepia(image, op.strength)

    register_op(OpSpec("sepia", SepiaOp, apply, frame_apply=frame, cost=0.02))

Plug-in modules import only this module (never ``app.imaging.pipeline``), so import order can't bite.
"""
from __future__ import annotations

import importlib
import json
import logging
import pkgutil
import threading
import typing
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any

import numpy as np
from PIL import Image, ImageColor
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

log = logging.getLogger("heyp.ops")


# ------------------------------------------------------------------ errors
class PipelineError(Exception):
    """A processing failure with an HTTP status (413 too large, 503 feature unavailable, …)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail

    def __reduce__(self) -> tuple:  # keeps status/detail across process boundaries
        return _rebuild_error, (type(self), self.args, dict(self.__dict__))


def _rebuild_error(cls: type, args: tuple, state: dict) -> BaseException:
    exc = cls.__new__(cls)
    BaseException.__init__(exc, *args)
    exc.__dict__.update(state)
    return exc


class FeatureUnavailable(PipelineError):
    def __init__(self, detail: str) -> None:
        super().__init__(503, detail)


# ------------------------------------------------------------------ shared model helpers
def _check_color(value: str) -> str:
    value = value.strip()
    if value.lower() == "transparent":
        return "transparent"
    try:
        ImageColor.getrgb(value)
    except ValueError:
        raise ValueError(
            f"invalid colour {value!r} (use #rgb, #rrggbb, #rrggbbaa, a CSS name or 'transparent')"
        ) from None
    return value


Color = Annotated[str, Field(min_length=1, max_length=64), AfterValidator(_check_color)]


class OpModel(BaseModel):
    """Base for op models: unknown fields are rejected and NaN/inf are invalid."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


# ------------------------------------------------------------------ context
@dataclass
class Ctx:
    """State threaded through the ops of one pipeline run."""

    image: Image.Image
    dpi: tuple[float, float] | None
    exif: bytes | None
    icc_profile: bytes | None
    source_format: str
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    cache_key: str = ""
    max_pixels: int = 0
    alive: Callable[[], bool] | None = None  # False once the upload is deleted (memo puts are undone)
    # ---- animation — frames[0] is ``image``
    frames: list[Image.Image] | None = None
    durations: list[int] | None = None
    loop: int | None = 0     # 0 = forever, None = play once
    frame_index: int = 0
    # ---- v2: jobs — progress reporting and cooperative cancellation (no-ops outside a job)
    on_progress: Callable[[float, str], None] | None = None
    cancel_check: Callable[[], None] | None = None
    # ---- v2: other uploads an op may reference (e.g. a watermark image): id → Loaded
    resolve_file: Callable[[str], Any] | None = None

    def warn(self, message: str | None) -> None:
        if message and message not in self.warnings:
            self.warnings.append(message)

    def progress(self, fraction: float, message: str = "") -> None:
        if self.on_progress is not None:
            self.on_progress(max(0.0, min(1.0, float(fraction))), message)

    def check_cancel(self) -> None:
        if self.cancel_check is not None:
            self.cancel_check()

    @property
    def animated(self) -> bool:
        return self.frames is not None and len(self.frames) > 1

    def load_file(self, file_id: str) -> Any:
        """Another upload referenced by an op (``io.Loaded``); 422 when ops can't reference files here."""
        if self.resolve_file is None:
            raise ValueError("this op needs another uploaded file, which is not available here")
        return self.resolve_file(file_id)


# ------------------------------------------------------------------ specs
PlanSize = Callable[[tuple[float, float], Any, "float | None"], "tuple[tuple[float, float], float | None]"]


@dataclass(frozen=True)
class OpSpec:
    name: str
    model: type[BaseModel]
    apply: Callable[[Ctx, Any, Sequence[BaseModel]], None]          # (ctx, op, preceding ops) → mutates ctx
    frame_apply: Callable[[Ctx, Any, Image.Image], Image.Image] | None = None  # (ctx, op, frame) → frame
    geometry: bool = False   # changes the picture's geometry (drops passport guides measured before it)
    heavy: bool = False      # slow (e.g. ONNX upscaling): too slow for live previews at full size
    # ---- planning hints for admission and the work budget (pipeline.plan); all optional
    cost: float = 0.02       # CPU seconds per megapixel (of the larger of input and output)
    fixed_cost: float = 0.0  # CPU seconds per call (model start-up, analysis)
    extra_bytes: int = 4     # peak bytes per input pixel above the input/output images themselves
    fixed_bytes: int = 0     # peak bytes independent of the image size (e.g. an ONNX session)
    max_pixels: int | None = None  # inputs larger than this are refused with 413
    label: str = ""          # human name in messages ("Background removal")
    plan_size: PlanSize | None = None  # (size, op, dpi) → (upper-bound output size, dpi after the op)

    @property
    def title(self) -> str:
        return self.label or self.name.replace("_", " ").capitalize()


_lock = threading.RLock()
_ops: dict[str, OpSpec] = {}
_fallback: set[str] = set()
PLUGIN_ERRORS: dict[str, str] = {}
_loaded = False
PLUGIN_PACKAGE = "app.imaging.ops_ext"


def _literal_name(model: type[BaseModel]) -> str | None:
    info = model.model_fields.get("op")
    if info is None:
        return None
    args = typing.get_args(info.annotation)
    if typing.get_origin(info.annotation) is typing.Literal and len(args) == 1 and isinstance(args[0], str):
        return args[0]
    return None


def register_op(spec: OpSpec, *, replace: bool = False, fallback: bool = False) -> OpSpec:
    """Register an op. ``fallback=True`` (the v1 compatibility shim) yields to any real registration.

    Raises ValueError when the model's ``op`` field isn't ``Literal[spec.name]`` or the name is taken.
    """
    if not isinstance(spec.model, type) or not issubclass(spec.model, BaseModel):
        raise ValueError(f"op {spec.name!r}: model must be a pydantic model")
    if _literal_name(spec.model) != spec.name:
        raise ValueError(f"op {spec.name!r}: the model needs a field op: Literal[{spec.name!r}]")
    if spec.model.model_config.get("extra") != "forbid":
        raise ValueError(f"op {spec.name!r}: the model must forbid extra fields (subclass OpModel)")
    if not callable(spec.apply) or (spec.frame_apply is not None and not callable(spec.frame_apply)):
        raise ValueError(f"op {spec.name!r}: apply/frame_apply must be callable")
    with _lock:
        existing = _ops.get(spec.name)
        if existing is not None:
            if fallback:
                return existing  # a real implementation is already there
            if not (replace or spec.name in _fallback
                    or (existing.apply.__module__, existing.apply.__qualname__)
                    == (spec.apply.__module__, spec.apply.__qualname__)):
                raise ValueError(f"op {spec.name!r} is already registered by {existing.apply.__module__}")
        _ops[spec.name] = spec
        if fallback:
            _fallback.add(spec.name)
        else:
            _fallback.discard(spec.name)
    return spec


def get_op(name: str) -> OpSpec:
    with _lock:
        return _ops[name]


def all_ops() -> list[OpSpec]:
    with _lock:
        return list(_ops.values())


def is_fallback(name: str) -> bool:
    with _lock:
        return name in _fallback


def load_plugins() -> dict[str, str]:
    """Import every module in ``app/imaging/ops_ext/`` once. A module that fails to import is logged
    and recorded in ``PLUGIN_ERRORS`` (its ops are simply missing) instead of taking the app down."""
    global _loaded
    with _lock:
        if _loaded:
            return dict(PLUGIN_ERRORS)
        _loaded = True
    try:
        package = importlib.import_module(PLUGIN_PACKAGE)
    except Exception as exc:  # no package → no plug-ins
        PLUGIN_ERRORS[PLUGIN_PACKAGE] = f"{type(exc).__name__}: {exc}"
        return dict(PLUGIN_ERRORS)
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda m: (m.name != "core", m.name)):
        if info.name.startswith("_"):
            continue
        name = f"{PLUGIN_PACKAGE}.{info.name}"
        try:
            importlib.import_module(name)
        except Exception as exc:
            PLUGIN_ERRORS[name] = f"{type(exc).__name__}: {exc}"
            log.exception("image op plug-in %s failed to load", name)
    return dict(PLUGIN_ERRORS)


# ------------------------------------------------------------------ helpers for op implementations
def check_pixels(width: float, height: float, limit: int, what: str) -> None:
    """413 when ``width × height`` exceeds ``limit`` (call before allocating a big result)."""
    pixels = width * height
    if pixels > limit:
        raise PipelineError(413, f"{what}: result would be {pixels / 1e6:.0f} MP, above the {limit / 1e6:.0f} MP limit")


def _nbytes(value: Any) -> int:
    """Array memory held by a cache value (a mask, or an object with a ``mask`` array)."""
    if isinstance(value, np.ndarray):
        return int(value.nbytes)
    mask = getattr(value, "mask", None)
    return int(mask.nbytes) if isinstance(mask, np.ndarray) else 0


class LRU:
    """Tiny thread-safe LRU for expensive per-image analysis (faces, masks).

    Bounded by ``maxsize`` entries and ``max_bytes`` of array data; an entry larger than ``max_bytes``
    is not stored at all.
    """

    def __init__(self, maxsize: int, max_bytes: int | None = None) -> None:
        self.maxsize = maxsize
        self.max_bytes = max_bytes
        self._data: OrderedDict[str, Any] = OrderedDict()
        self._sizes: dict[str, int] = {}
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            value = self._data.get(key)
            if value is not None:
                self._data.move_to_end(key)
            return value

    def put(self, key: str, value: Any) -> None:
        size = _nbytes(value)
        with self._lock:
            self._pop(key)
            if self.max_bytes is not None and size > self.max_bytes:
                return
            self._data[key] = value
            self._sizes[key] = size
            self._bytes += size
            while len(self._data) > self.maxsize or self._over_budget():
                self._pop(next(iter(self._data)))

    def drop_prefix(self, prefix: str) -> None:
        with self._lock:
            for key in [k for k in self._data if k.startswith(prefix)]:
                self._pop(key)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._sizes.clear()
            self._bytes = 0

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._data)

    @property
    def nbytes(self) -> int:
        with self._lock:
            return self._bytes

    def _over_budget(self) -> bool:
        return self.max_bytes is not None and self._bytes > self.max_bytes

    def _pop(self, key: str) -> None:
        self._data.pop(key, None)
        self._bytes -= self._sizes.pop(key, 0)


_caches: dict[str, LRU] = {}


def cache(name: str, maxsize: int = 4, max_bytes: int | None = None) -> LRU:
    """A named analysis cache shared by every op (created on first use; later calls get the same one)."""
    with _lock:
        existing = _caches.get(name)
        if existing is None:
            existing = _caches[name] = LRU(maxsize, max_bytes)
        return existing


def forget(cache_key: str) -> None:
    """Drop every memoised analysis of one upload (its delete/expiry hook)."""
    if not cache_key:
        return
    with _lock:
        caches = list(_caches.values())
    for lru in caches:
        lru.drop_prefix(cache_key + "|")


def clear_caches() -> None:
    with _lock:
        caches = list(_caches.values())
    for lru in caches:
        lru.clear()


def memo_key(ctx: Ctx, kind: str, prefix: Sequence[BaseModel]) -> str | None:
    """``<upload id>|<kind>|<json of the preceding ops>`` — an edited image never reuses a stale analysis."""
    if not ctx.cache_key:
        return None
    chain = json.dumps([op.model_dump(mode="json") for op in prefix], sort_keys=True, separators=(",", ":"))
    return f"{ctx.cache_key}|{kind}|{chain}"


def memo_put(ctx: Ctx, lru: LRU, key: str, value: Any) -> None:
    """Store, then undo if the upload was deleted meanwhile (its forget() may already have run)."""
    lru.put(key, value)
    if ctx.alive is not None and not ctx.alive():
        forget(ctx.cache_key)
