"""Tests for "Remove background (any subject)": app.imaging.matting (BiRefNet_lite), the lazy ONNX sessions
(app.imaging.ortsession) and the ``background`` op's engines (app.imaging.ops_ext.ai)."""
from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageFilter
from pydantic import ValidationError

from app.imaging import io as imgio, matting, ortsession, pipeline, segment
from app.imaging.opreg import FeatureUnavailable
from app.schemas import OPS_ADAPTER, OutputSpec

FIXTURES = Path(__file__).parent / "fixtures"
ASTRONAUT = FIXTURES / "astronaut.png"
REFERENCE = FIXTURES / "ai" / "astronaut_birefnet_mask.png"

needs_model = pytest.mark.skipif(not matting.available("fp32"), reason="BiRefNet model not installed")


def parse_op(data: dict):
    return OPS_ADAPTER.validate_python([data])[0]


def loaded(img: Image.Image, fmt: str = "png") -> imgio.Loaded:
    return imgio.Loaded(image=img, format=fmt, exif=None, icc_profile=None, dpi=None, frames=1, bit_depth=8,
                        orientation=1)


def iou(a: np.ndarray, b: np.ndarray) -> float:
    return float((a & b).sum() / max(1, (a | b).sum()))


def ball_scene(w: int = 640, h: int = 480) -> tuple[Image.Image, np.ndarray]:
    """A shaded red ball on a noisy gradient: the exact foreground mask is known."""
    rng = np.random.default_rng(1)
    bg = np.clip(rng.normal(0, 18, (h, w, 3)) + np.linspace(90, 160, w)[None, :, None], 0, 255).astype(np.uint8)
    arr = np.asarray(Image.fromarray(bg).filter(ImageFilter.GaussianBlur(2))).copy()
    yy, xx = np.mgrid[0:h, 0:w]
    cx, cy, r = w // 2, int(h * 0.52), int(min(w, h) * 0.31)
    inside = (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r
    shade = np.clip(1.15 - np.sqrt((xx - cx + r / 3) ** 2 + (yy - cy + r / 3) ** 2) / (1.6 * r), 0.25, 1.0)
    ball = np.stack([230 * shade, 60 * shade, 40 * shade], -1).astype(np.uint8)
    arr[inside] = ball[inside]
    return Image.fromarray(arr), inside


@pytest.fixture(scope="module")
def astronaut() -> Image.Image:
    if not ASTRONAUT.is_file():
        pytest.skip("tests/fixtures/astronaut.png missing")
    return Image.open(ASTRONAUT).convert("RGB")


# ------------------------------------------------------------------ configuration / sessions (no model)
@pytest.mark.parametrize("raw, expected", [("", "fp32"), ("int8", "int8"), (" INT8 ", "int8"), ("fp16", "fp32"),
                                           ("../x", "fp32")])
def test_variant_env(monkeypatch, raw, expected):
    monkeypatch.setenv("HEYP_MATTING_MODEL", raw)
    assert matting.variant() == expected


def test_missing_models_are_unavailable_without_loading(tmp_path, monkeypatch):
    monkeypatch.setenv("HEYP_MODELS_DIR", str(tmp_path))
    s = ortsession.ModelSession("nothing.onnx", label="nothing")
    assert not s.available() and not s.loaded()
    with pytest.raises(ortsession.ModelUnavailable, match="nothing model is not installed"):
        s.get()
    (tmp_path / "small.onnx").write_bytes(b"x" * 10)
    assert not ortsession.ModelSession("small.onnx", min_bytes=100).available()  # implausibly small: not even tried


def test_session_is_created_once_under_concurrency(tmp_path, monkeypatch):
    import onnxruntime as ort

    (tmp_path / "m.onnx").write_bytes(b"\0" * 200_000)
    monkeypatch.setenv("HEYP_MODELS_DIR", str(tmp_path))
    created: list[object] = []
    gate = threading.Event()

    class FakeSession:
        def __init__(self, path, sess_options=None, providers=None):
            gate.wait(2)
            created.append(sess_options)

    monkeypatch.setattr(ort, "InferenceSession", FakeSession)
    s = ortsession.ModelSession("m.onnx", low_memory=True)
    got: list[object] = []
    threads = [threading.Thread(target=lambda: got.append(s.get())) for _ in range(8)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(5)
    assert len(created) == 1 and len({id(g) for g in got}) == 1 and len(got) == 8
    opts = created[0]
    assert opts.enable_cpu_mem_arena is False and opts.enable_mem_pattern is False  # BiRefNet: 4.3 → 1.6 GB peak
    assert 1 <= opts.intra_op_num_threads <= ortsession.MAX_THREADS


@pytest.mark.parametrize("raw, ok", [("2", 2), ("0", None), ("x", None), ("999", 64)])
def test_thread_count(monkeypatch, raw, ok):
    monkeypatch.setenv("HEYP_AI_THREADS", raw)
    expected = ok if ok is not None else max(1, min(ortsession.MAX_THREADS, ortsession.available_cpus()))
    assert ortsession.ai_threads() == expected


def test_preprocess_normalises_and_flattens_alpha():
    img = Image.new("RGBA", (300, 200), (0, 0, 0, 0))  # fully transparent → white
    x = matting.preprocess(img)
    assert x.shape == (1, 3, 1024, 1024) and x.dtype == np.float32 and x.flags.c_contiguous
    white = (1.0 - np.array([0.485, 0.456, 0.406])) / np.array([0.229, 0.224, 0.225])
    assert np.allclose(x[0, :, 500, 500], white, atol=1e-4)
    for mode in ("L", "LA", "P", "CMYK", "I;16", "1"):
        assert matting.preprocess(Image.new(mode, (20, 10))).shape == (1, 3, 1024, 1024)


def test_object_mask_rejects_bad_arguments():
    with pytest.raises(TypeError):
        matting.raw_mask(np.zeros((4, 4, 3)))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="at least 1×1"):
        matting.object_mask(Image.new("RGB", (4, 4)), size=(0, 4))


# ------------------------------------------------------------------ the background op (engines faked)
def fake_masks(monkeypatch, *, object_ok=True, person_ok=True) -> dict[str, list]:
    calls: dict[str, list] = {"object": [], "person": []}

    def half(img, **_kw):
        m = np.zeros((img.height, img.width), np.float32)
        m[:, : img.width // 2] = 1.0
        return m

    monkeypatch.setattr(matting, "available", lambda name=None: object_ok)
    monkeypatch.setattr(segment, "available", lambda: person_ok)
    monkeypatch.setattr(matting, "object_mask", lambda img, **kw: calls["object"].append(img.size) or half(img))
    monkeypatch.setattr(segment, "person_mask", lambda img, **kw: calls["person"].append(img.size) or half(img))
    pipeline.clear_caches()
    return calls


def test_v1_json_still_validates_and_engine_is_checked():
    assert parse_op({"op": "background", "mode": "remove"}).engine == "auto"
    assert parse_op({"op": "background", "mode": "replace", "color": "#00ff00", "engine": "person"}).engine == "person"
    for bad in ({"op": "background", "mode": "remove", "engine": "rembg"}, {"op": "background"},
                {"op": "background", "mode": "remove", "color": "not-a-colour"},
                {"op": "background", "mode": "remove", "model": "u2net"}):
        with pytest.raises(ValidationError):
            parse_op(bad)


@pytest.mark.parametrize("engine, object_ok, person_ok, used, warned", [
    ("auto", True, True, "object", False),
    ("auto", False, True, "person", True),
    ("object", True, False, "object", False),
    ("person", True, True, "person", False),
])
def test_engine_resolution(monkeypatch, engine, object_ok, person_ok, used, warned):
    calls = fake_masks(monkeypatch, object_ok=object_ok, person_ok=person_ok)
    ctx = pipeline.run(loaded(Image.new("RGB", (40, 20), (200, 10, 10))),
                       [{"op": "background", "mode": "remove", "engine": engine}])
    assert len(calls[used]) == 1 and sum(len(v) for v in calls.values()) == 1
    assert ctx.image.mode == "RGBA" and ctx.image.getpixel((35, 10))[3] == 0 and ctx.image.getpixel((5, 10))[3] == 255
    assert bool(ctx.warnings) is warned


@pytest.mark.parametrize("engine, object_ok, person_ok", [
    ("auto", False, False), ("object", False, True), ("person", True, False)])
def test_unavailable_engines_are_503(monkeypatch, engine, object_ok, person_ok):
    fake_masks(monkeypatch, object_ok=object_ok, person_ok=person_ok)
    with pytest.raises(FeatureUnavailable) as info:
        pipeline.run(loaded(Image.new("RGB", (40, 20))), [{"op": "background", "mode": "remove", "engine": engine}])
    assert info.value.status == 503 and "unavailable" in info.value.detail


def test_model_failing_to_load_is_503(monkeypatch):
    fake_masks(monkeypatch)

    def broken(img, **kw):
        raise ortsession.ModelUnavailable("could not load the background-removal model (InvalidProtobuf)")

    monkeypatch.setattr(matting, "object_mask", broken)
    with pytest.raises(FeatureUnavailable, match="could not load"):
        pipeline.run(loaded(Image.new("RGB", (40, 20))), [{"op": "background", "mode": "remove"}])


def test_masks_are_memoised_per_image_engine_and_prefix(monkeypatch):
    calls = fake_masks(monkeypatch)
    src = loaded(Image.new("RGB", (40, 20), (200, 10, 10)))
    ops = [{"op": "background", "mode": "remove"}]
    pipeline.run(src, ops, cache_key="a1")
    second = pipeline.run(src, [{"op": "background", "mode": "replace", "color": "#00ff00"}], cache_key="a1")
    assert len(calls["object"]) == 1 and second.image.getpixel((35, 10)) == (0, 255, 0)
    pipeline.run(src, [{"op": "background", "mode": "remove", "engine": "person"}], cache_key="a1")
    assert len(calls["person"]) == 1  # another engine → its own mask
    pipeline.run(src, [{"op": "flip", "direction": "horizontal"}] + ops, cache_key="a1")
    assert len(calls["object"]) == 2  # different preceding ops → new analysis
    pipeline.run(src, ops)  # no cache key → never memoised
    assert len(calls["object"]) == 3
    pipeline.forget("a1")
    pipeline.run(src, ops, cache_key="a1")
    assert len(calls["object"]) == 4
    pipeline.run(src, ops, cache_key="gone", alive=lambda: False)  # deleted meanwhile → nothing kept
    assert not any(k.startswith("gone|") for k in pipeline._mask_cache.keys())


def test_background_op_size_cap(monkeypatch):
    fake_masks(monkeypatch)
    with pytest.raises(pipeline.PipelineError) as info:
        pipeline.run(loaded(Image.new("RGB", (8000, 7000))), [{"op": "background", "mode": "remove"}],
                     max_pixels=100_000_000)
    assert info.value.status == 413 and "Background removal works on images up to 50 MP" in info.value.detail


def test_plan_charges_the_model_once():
    p = pipeline.plan((1000, 1000), [{"op": "background", "mode": "remove"}], OutputSpec(format="png"))
    assert p.work >= 11 and p.peak_bytes >= matting.PEAK_BYTES


# ------------------------------------------------------------------ model-backed quality
@needs_model
def test_mask_matches_a_known_subject():
    img, truth = ball_scene()
    mask = matting.object_mask(img)
    assert mask.shape == truth.shape and mask.dtype == np.float32 and 0.0 <= mask.min() and mask.max() <= 1.0
    assert iou(mask > 0.5, truth) >= 0.97


@needs_model
def test_mask_matches_the_reference_on_a_portrait(astronaut):
    """Regression guard for pre/post-processing: the astronaut mask vs the stored fp32 reference."""
    if not REFERENCE.is_file():
        pytest.skip("tests/fixtures/ai/astronaut_birefnet_mask.png missing")
    ref = np.asarray(Image.open(REFERENCE)) > 127
    mask = matting.object_mask(astronaut, size=(256, 256), name="fp32") > 0.5
    assert iou(mask, ref) >= 0.98
    assert 0.3 < mask.mean() < 0.55  # the person, not the flag / shuttle background


@pytest.mark.skipif(not matting.available("int8"), reason="optional BiRefNet int8 variant not installed")
def test_int8_variant_agrees_with_fp32(astronaut):
    img, truth = ball_scene(480, 360)
    assert iou(matting.object_mask(img, name="int8") > 0.5, truth) >= 0.97
    ref = np.asarray(Image.open(REFERENCE)) > 127
    assert iou(matting.object_mask(astronaut, size=(256, 256), name="int8") > 0.5, ref) >= 0.95


@needs_model
def test_remove_background_end_to_end(astronaut):
    pipeline.clear_caches()
    ctx = pipeline.run(loaded(astronaut), [{"op": "background", "mode": "remove", "engine": "object"}],
                       cache_key="astro")
    out = ctx.image
    assert out.mode == "RGBA" and out.size == astronaut.size
    alpha = np.asarray(out.getchannel("A"))
    assert alpha[:30, :30].max() < 20 and alpha[200:260, 200:260].min() > 230  # corner gone, face kept
    data, fmt, _ = pipeline.render(ctx, {"format": "png"})
    assert fmt == "png" and data[:8] == b"\x89PNG\r\n\x1a\n"


@needs_model
def test_tiny_and_extreme_aspect_inputs():
    for size in ((1, 1), (3, 900), (900, 3)):
        assert matting.object_mask(Image.new("RGB", size, (120, 30, 30))).shape == (size[1], size[0])
