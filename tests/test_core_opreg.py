"""app.imaging.opreg + the pipeline's registry dispatch: op plug-ins, the Op union, animations and the
frame-bomb guard, planning hints."""
from __future__ import annotations

import io
from typing import Literal

import pytest
from PIL import Image, ImageOps
from pydantic import BaseModel

from app import schemas
from app.imaging import io as imgio, opreg, pipeline
from app.imaging.opreg import Ctx, OpModel, OpSpec, register_op
from app.schemas import OutputSpec


# ------------------------------------------------------------------ test ops (models are not in the union:
# the pipeline accepts model instances directly)
class InvertOp(OpModel):
    op: Literal["zz_invert"]


class StillOnlyOp(OpModel):
    op: Literal["zz_still"]


class LooseOp(BaseModel):
    op: Literal["zz_loose"]


class MismatchOp(OpModel):
    op: Literal["something_else"]


def _invert(ctx: Ctx, op, prefix) -> None:
    ctx.progress(0.5, "inverting")
    ctx.image = ImageOps.invert(ctx.image.convert("RGB"))


def _invert_frame(ctx: Ctx, op, frame: Image.Image) -> Image.Image:
    return ImageOps.invert(frame.convert("RGB"))


def _still(ctx: Ctx, op, prefix) -> None:
    ctx.meta["still_ran"] = True


@pytest.fixture(autouse=True)
def _ops():
    register_op(OpSpec("zz_invert", InvertOp, _invert, frame_apply=_invert_frame, label="Invert"))
    register_op(OpSpec("zz_still", StillOnlyOp, _still, label="Still effect"))
    yield
    with opreg._lock:
        for name in ("zz_invert", "zz_still", "zz_tmp"):
            opreg._ops.pop(name, None)
            opreg._fallback.discard(name)


def loaded(img: Image.Image, fmt: str = "PNG", **kw) -> imgio.Loaded:
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return imgio.load(buf.getvalue())


def gif_bytes(frames: int = 3, size=(40, 20)) -> bytes:
    images = [Image.new("RGB", size, (60 * i, 10, 200)) for i in range(frames)]
    buf = io.BytesIO()
    images[0].save(buf, "GIF", save_all=True, append_images=images[1:], duration=[100, 200, 300][:frames] or 100,
                   loop=0)
    return buf.getvalue()


# ------------------------------------------------------------------ registry rules
def test_registration_rules():
    with pytest.raises(ValueError, match="Literal"):
        register_op(OpSpec("zz_tmp", MismatchOp, _still))
    with pytest.raises(ValueError, match="forbid extra"):
        register_op(OpSpec("zz_loose", LooseOp, _still))
    with pytest.raises(ValueError, match="pydantic"):
        register_op(OpSpec("zz_tmp", dict, _still))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="already registered"):
        register_op(OpSpec("zz_invert", InvertOp, _still))
    register_op(OpSpec("zz_invert", InvertOp, _invert))  # the same function again (reload) is fine
    register_op(OpSpec("zz_invert", InvertOp, _still), replace=True)
    assert opreg.get_op("zz_invert").apply is _still


def test_fallbacks_yield_to_real_plugins():
    class TmpOp(OpModel):
        op: Literal["zz_tmp"]

    shim = register_op(OpSpec("zz_tmp", TmpOp, _still), fallback=True)
    assert opreg.is_fallback("zz_tmp")
    real = register_op(OpSpec("zz_tmp", TmpOp, _invert))
    assert opreg.get_op("zz_tmp") is real and not opreg.is_fallback("zz_tmp")
    assert register_op(OpSpec("zz_tmp", TmpOp, _still), fallback=True) is real  # a late shim changes nothing
    assert shim is not real


def test_every_plugin_loaded_and_the_union_covers_them():
    assert opreg.load_plugins() == {}, "an ops_ext module failed to import"
    names = set(schemas.OP_NAMES)
    v1 = {"rotate", "flip", "crop", "crop_aspect", "trim", "resize", "pad", "adjust", "background", "passport",
          "sheet"}
    assert v1 <= names
    assert {spec.name for spec in opreg.all_ops()} >= v1
    # the v1 JSON keeps validating exactly as before; plug-in fields are optional
    ops = schemas.OPS_ADAPTER.validate_python([{"op": "rotate", "angle": 90}, {"op": "background", "mode": "remove"}])
    assert ops[0].angle == 90 and ops[1].mode == "remove"
    with pytest.raises(Exception):
        schemas.OPS_ADAPTER.validate_python([{"op": "rotate", "angle": 90, "bogus": 1}])


def test_v1_passport_and_sheet_still_run_through_the_shim():
    assert opreg.is_fallback("passport") or opreg.get_op("passport").apply.__module__ != "app.imaging.pipeline"
    assert not opreg.is_fallback("rotate")  # migrated to ops_ext/core.py


# ------------------------------------------------------------------ dispatch and context
def test_pipeline_dispatches_registered_ops_with_progress_and_cancel():
    seen: list[tuple[float, str]] = []
    ctx = pipeline.run(loaded(Image.new("RGB", (10, 10), (0, 0, 0))), [InvertOp(op="zz_invert")],
                       on_progress=lambda f, m: seen.append((f, m)))
    assert ctx.image.getpixel((0, 0)) == (255, 255, 255) and seen == [(0.5, "inverting")]

    class Stop(Exception):
        pass

    def cancel() -> None:
        raise Stop()

    with pytest.raises(Stop):
        pipeline.run(loaded(Image.new("RGB", (4, 4))), [InvertOp(op="zz_invert")], cancel_check=cancel)
    with pytest.raises(ValueError, match="unknown op"):
        pipeline._spec("zz_never_registered")


def test_ops_can_reference_other_files_only_with_a_resolver():
    ctx = Ctx(image=Image.new("RGB", (1, 1)), dpi=None, exif=None, icc_profile=None, source_format="png")
    with pytest.raises(ValueError, match="not available here"):
        ctx.load_file("a" * 32)
    ctx.resolve_file = lambda fid: f"loaded {fid[:2]}"
    assert ctx.load_file("ab" + "0" * 30) == "loaded ab"


# ------------------------------------------------------------------ animations
def test_animated_gif_is_processed_frame_by_frame():
    data = gif_bytes(3)
    anim = pipeline.load_animation(data)
    source = imgio.load(data)
    assert source.frames == 3 and pipeline.wants_animation(source, OutputSpec(format="original"))
    ops = schemas.OPS_ADAPTER.validate_python([{"op": "rotate", "angle": 90}]) + [InvertOp(op="zz_invert")]
    ctx = pipeline.run(source, ops, animation=anim)
    assert ctx.animated and len(ctx.frames) == 3 and ctx.image.size == (20, 40)
    out, fmt, info = pipeline.render(ctx, OutputSpec(format="original"))
    assert fmt == "gif" and info["frames"] == 3
    with Image.open(io.BytesIO(out)) as im:
        assert im.n_frames == 3 and im.size == (20, 40)


def test_an_op_without_frame_support_keeps_the_first_frame_with_a_warning():
    data = gif_bytes(3)
    ctx = pipeline.run(imgio.load(data), [StillOnlyOp(op="zz_still")], animation=pipeline.load_animation(data))
    assert not ctx.animated and ctx.meta["still_ran"]
    assert ctx.warnings == ["Still effect can't be applied to each frame of an animation — only the first frame "
                            "was kept"]


def test_explicit_still_outputs_do_not_want_the_animation():
    source = imgio.load(gif_bytes(3))
    assert not pipeline.wants_animation(source, OutputSpec(format="png"))
    assert not pipeline.wants_animation(source, OutputSpec(format="jpeg"))
    assert pipeline.wants_animation(source, OutputSpec(format="webp"))
    assert not pipeline.animated_output("jpeg", 3, OutputSpec(format="original"))
    assert not pipeline.animated_output("gif", 1, OutputSpec(format="gif"))


def test_frame_bomb_guard_before_decoding():
    with pytest.raises(pipeline.PipelineError) as frames:
        pipeline.check_animation_size(5000, 10, 10, max_frames=1000)
    assert frames.value.status == 413 and "5000 frames" in frames.value.detail
    with pytest.raises(pipeline.PipelineError) as pixels:
        pipeline.check_animation_size(100, 7000, 7000)  # 4.9 GP of decoded frames
    assert pixels.value.status == 413 and "too large" in pixels.value.detail
    pipeline.check_animation_size(100, 100, 100)
    with pytest.raises(pipeline.PipelineError):
        pipeline.load_animation(gif_bytes(3), max_pixels=1000)  # 3 × 800 px


def test_frames_that_grow_too_large_are_stopped():
    data = gif_bytes(3)
    ops = schemas.OPS_ADAPTER.validate_python([{"op": "resize", "mode": "scale", "scale": 400}])
    with pytest.raises(pipeline.PipelineError) as info:
        pipeline.run(imgio.load(data), ops, animation=pipeline.load_animation(data), max_anim_pixels=20_000)
    assert info.value.status == 413


# ------------------------------------------------------------------ planning
def test_plan_uses_plugin_estimates_and_the_v1_table_for_shims():
    out = OutputSpec(format="jpeg")
    bg = pipeline.plan((1000, 1000), [{"op": "background", "mode": "remove"}], out)
    spec = opreg.get_op("background")
    if not opreg.is_fallback("background"):
        assert bg.work >= spec.fixed_cost and bg.peak_bytes >= spec.fixed_bytes
    rot = pipeline.plan((1000, 1000), [{"op": "rotate", "angle": 90}], out)
    assert rot.work == pytest.approx(0.03 + 0.04 + 0.03)
    anim = pipeline.plan((100, 100), [{"op": "rotate", "angle": 90}], OutputSpec(format="gif"), frames=10)
    still = pipeline.plan((100, 100), [{"op": "rotate", "angle": 90}], OutputSpec(format="gif"))
    assert anim.work > 9 * still.work * 0.9 and anim.peak_bytes > still.peak_bytes
