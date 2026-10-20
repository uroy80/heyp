"""``img.process`` — heyp's export pipeline as a job (heavy lane, SSE progress, cancel).

The options are ``POST /api/process`` without ``ids`` (those are the job's ``file_ids``):
``{"ops": [...], "ops_by_id": {id: [...]}, "output": {...}}``. Every file goes through the same
``pipeline.run`` + ``pipeline.render`` as the synchronous endpoint, but inside a job, so a long export
(upscale, background removal, a large batch) reports progress per file (and per upscale tile) and never
has to finish inside one HTTP request — Cloudflare cuts origin responses after 100 s, Apache's default
proxy timeout is 60 s.

Results: one file per input (named as ``/api/process`` names them) plus the job's ZIP when there are
several; ``output.format = "pdf"`` with ``combine_pdf`` and several inputs gives one PDF instead.
Each file holds a slot of the shared job-memory budget only while it is processed, like
``/api/process`` does.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core import registry
from app.core.errors import ToolError, error_info
from app.imaging import io as imgio, pipeline
from app.schemas import OpList, OutputSpec
from app.store import StoreError, clean_name

MAX_PROCESS_FILES = 500           # structural cap; HEYP_MAX_FILES (the app's setting) is checked when it runs
WORK_BUDGET = 3600.0              # estimated CPU seconds per job (same as /api/process; jobs time out at 1800 s)
TIMEOUT_S = 1800


class ProcessOptions(BaseModel):
    """``ProcessRequest`` minus ``ids``."""

    model_config = ConfigDict(extra="forbid")

    ops: OpList = Field(default_factory=list)
    ops_by_id: dict[str, OpList] | None = None
    output: OutputSpec = Field(default_factory=OutputSpec)

    @model_validator(mode="after")
    def _keys(self) -> ProcessOptions:
        if self.ops_by_id and len(self.ops_by_id) > MAX_PROCESS_FILES:
            raise ValueError("ops_by_id has too many entries")
        return self

    def ops_for(self, file_id: str) -> list:
        if self.ops_by_id and file_id in self.ops_by_id:
            return self.ops_by_id[file_id]
        return self.ops


def _frames(fmt: str | None, frames: int, output: OutputSpec) -> int:
    return frames if pipeline.animated_output(fmt, frames, output) else 1


def plan_all(items: list[Any], opts: ProcessOptions, settings: Any) -> list[pipeline.Plan]:
    """Estimate every input before any work (frame-bomb guard, op size limits, the work budget) → ToolError."""
    plans = []
    multi = len(items) > 1
    for item in items:
        width, height = getattr(item, "width", None), getattr(item, "height", None)
        meta = getattr(item, "meta", None) or {}
        if not (width and height):
            side = int(settings.max_pixels ** 0.5)
            width = height = side
        try:
            frames = _frames(item.format, int(meta.get("frames") or 1), opts.output)
            if frames > 1:
                pipeline.check_animation_size(frames, int(width), int(height), max_frames=settings.max_anim_frames,
                                              max_pixels=settings.max_anim_pixels)
            plans.append(pipeline.plan((int(width), int(height)), opts.ops_for(item.id), opts.output,
                                       source_format=item.format or "jpeg", frames=frames))
        except ToolError:
            raise
        except Exception as exc:
            info = error_info(exc)
            if info is None:
                raise
            prefix = f"{item.name}: " if multi else ""
            raise ToolError(int(info["status"]), f"{prefix}{info['detail']}") from None
    work = sum(p.work for p in plans)
    if work > WORK_BUDGET:
        raise ToolError(413, f"This export is too much work for one go (about {work / 60:.0f} min of processing; "
                             f"the limit is {WORK_BUDGET / 60:.0f} min). Export fewer photos at once, or use "
                             "smaller sizes or a faster format.")
    return plans


def _validate(stored: list[Any], opts: BaseModel) -> None:
    from app.config import get_settings

    assert isinstance(opts, ProcessOptions)
    ids = {s.id for s in stored}
    if opts.ops_by_id and set(opts.ops_by_id) - ids:
        raise ToolError(422, "ops_by_id names files that are not in this job")
    plan_all(stored, opts, get_settings())


def _resolver(ctx: Any) -> Any:
    """Files an op references (a watermark logo): validated, pinned for the job, decoded via the store."""
    def resolve(file_id: str) -> Any:
        try:
            ctx.extra_input(file_id, ("image",))
            return ctx.load_image(file_id)
        except (ToolError, StoreError):
            raise ToolError(422, "A file used by this edit (e.g. a watermark image) was not found or has "
                                 "expired; upload it again") from None

    return resolve


def _still_warning(inp: Any, fmt: str, output: OutputSpec) -> str | None:
    """An animated input written to a format that can't animate: say that only the first frame was kept."""
    frames = int((inp.info or {}).get("frames") or 1)
    if frames > 1 and not pipeline.animated_output(inp.format, frames, output):
        return pipeline.still_warning(fmt)
    return None


def run_process(ctx: Any, opts: ProcessOptions) -> None:
    from app.api import output_name  # the same names as /api/process

    settings = ctx.settings
    inputs = list(ctx.inputs)
    total = len(inputs)
    if total > settings.max_files:
        raise ToolError(422, f"Too many photos for one export: {total} (the limit is {settings.max_files})")
    stored = [ctx.store.get(i.id) for i in inputs]
    plans = plan_all(stored, opts, settings)
    combine = opts.output.format == "pdf" and opts.output.combine_pdf and total > 1
    resolve = _resolver(ctx)
    pages: list[Any] = []
    pixels = 0
    first_dpi: tuple[float, float] | None = None

    for index, (inp, plan) in enumerate(zip(inputs, plans)):
        suffix = f" ({index + 1} of {total})" if total > 1 else ""

        def progress(frac: float, msg: str = "", _i: int = index, _s: str = suffix) -> None:
            ctx.progress((_i + max(0.0, min(1.0, float(frac))) * 0.95) / total, f"{msg or 'Processing'}{_s}")

        ctx.check_cancel()
        progress(0.0)
        with ctx.reserve_memory(plan.peak_bytes):
            loaded = ctx.load_image(inp.id)
            animation = None
            if not combine and pipeline.wants_animation(loaded, opts.output):
                animation = pipeline.load_animation(str(inp.path), check_cancel=ctx.check_cancel,
                                                    max_frames=settings.max_anim_frames,
                                                    max_pixels=settings.max_anim_pixels)
            pctx = pipeline.run(loaded, opts.ops_for(inp.id), cache_key=inp.id, max_pixels=settings.max_pixels,
                                alive=lambda i=inp.id: ctx.store.exists(i), animation=animation,
                                on_progress=progress, cancel_check=ctx.check_cancel, resolve_file=resolve,
                                max_anim_pixels=settings.max_anim_pixels)
            ctx.check_cancel()
            prefix = f"{inp.name}: " if total > 1 else ""
            if combine:
                img = pctx.image
                if opts.output.colorspace == "srgb":
                    img = imgio.to_srgb(img, pctx.icc_profile)
                pixels += img.width * img.height
                if pixels > settings.max_pixels:
                    raise ToolError(413, f"Too many pixels for one PDF (limit {settings.max_pixels / 1e6:.0f} MP); "
                                         "reduce the size or turn off 'Combine into one PDF'")
                pages.append(img)
                first_dpi = first_dpi or pctx.dpi
                for w in pctx.warnings:
                    ctx.warn(f"{prefix}{w}")
                continue
            progress(0.97, "Saving")
            data, fmt, info = pipeline.render(pctx, opts.output)
        warnings = list(info.get("warnings") or ())
        still = _still_warning(inp, fmt, opts.output)
        if still and still not in warnings:
            warnings.append(still)
        for w in warnings:
            ctx.warn(f"{prefix}{w}")
        name = output_name(inp.name, fmt, pctx.meta, opts.output.filename if total == 1 else None)
        ctx.add_output(data, name, imgio.mime_for(fmt), kind="image")
        del data, pctx

    if combine:
        ctx.progress(0.97, "Writing the PDF")
        dpi = opts.output.dpi or (round(first_dpi[0]) if first_dpi else pipeline.PDF_DEFAULT_DPI)
        with ctx.reserve_memory(4 * pixels):
            data = imgio.save_pdf(pages, dpi=dpi, background=opts.output.background)
        name = clean_name(f"{opts.output.filename or f'heyp-{total}-files'}.pdf")
        ctx.add_output(data, name, imgio.mime_for("pdf"), kind="pdf")
    ctx.data.update({"files": total, "format": opts.output.format, "combined": bool(combine)})
    ctx.progress(1.0, "Done")


PROCESS = registry.register(registry.Tool(
    id="img.process", title="Export photos", category="Export",
    summary="Apply heyp's edits to photos and save them in the chosen format, with progress for long exports.",
    inputs=("image",), options=ProcessOptions, run=run_process, min_files=1, max_files=MAX_PROCESS_FILES,
    lane="heavy", timeout_s=TIMEOUT_S, limits={"max_files": MAX_PROCESS_FILES, "memory_mb": 0},
    validate=_validate,
    outputs="One file per photo, named like a direct download; several → also a ZIP; PDF + combine → one PDF"))
