"""Tests for Blur faces / plates / areas: app.imaging.blur, the ``blur_regions`` op, ``GET /api/detect`` and
the heyp AI job tools in app/tools/img_ai.py (``img.blur``, ``img.upscale``, ``img.remove_background``)."""
from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from pydantic import ValidationError

from app.config import Settings
from app.imaging import blur, face, io as imgio, matting, pipeline, plates, segment, upscale
from app.imaging.blur import Region
from app.imaging.opreg import Ctx, get_op
from app.imaging.ortsession import ModelUnavailable
from app.schemas import OPS_ADAPTER
from app.store import FileStore
from app.tools import img_ai

FIXTURES = Path(__file__).parent / "fixtures"
ASTRONAUT = FIXTURES / "astronaut.png"
CAR = FIXTURES / "ai" / "car_plate.jpg"


def parse_op(data: dict):
    return OPS_ADAPTER.validate_python([data])[0]


def loaded(img: Image.Image, fmt: str = "png") -> imgio.Loaded:
    return imgio.Loaded(image=img, format=fmt, exif=None, icc_profile=None, dpi=None, frames=1, bit_depth=8,
                        orientation=1)


def checker(w: int = 200, h: int = 100, cell: int = 4) -> Image.Image:
    """Fine black/white checkerboard: maximal detail, so any blur is measurable."""
    yy, xx = np.mgrid[0:h, 0:w]
    arr = (((xx // cell) + (yy // cell)) % 2 * 255).astype(np.uint8)
    return Image.fromarray(np.stack([arr] * 3, -1))


def detail(arr: np.ndarray) -> float:
    a = arr.astype(np.float64)
    return float(np.abs(np.diff(a, axis=0)).mean() + np.abs(np.diff(a, axis=1)).mean())


# ------------------------------------------------------------------ blur_regions
@pytest.mark.parametrize("mode", blur.MODES)
def test_only_the_region_changes_and_detail_is_destroyed(mode):
    img = checker()
    out = blur.blur_regions(img, [{"x": 0.25, "y": 0.2, "w": 0.5, "h": 0.6}], mode=mode)
    a, b = np.asarray(img), np.asarray(out)
    assert out.size == img.size and out.mode == "RGB"
    inside = (slice(20, 80), slice(50, 150))
    outside = np.ones(a.shape[:2], bool)
    outside[inside] = False
    assert np.array_equal(a[outside], b[outside])  # nothing outside the box is touched
    assert detail(b[22:78, 52:148]) < 0.1 * detail(a[22:78, 52:148])  # unreadable inside


def test_solid_uses_the_colour_and_pixelate_makes_blocks():
    img = checker()
    solid = np.asarray(blur.blur_regions(img, [Region(0, 0, 0.5, 1)], mode="solid", color=(10, 20, 30, 255)))
    assert (solid[:, :100] == (10, 20, 30)).all()
    pix = np.asarray(blur.blur_regions(img, [Region(0, 0, 1, 1)], mode="pixelate", strength=1.0))
    block = blur.pixel_block(200, 1.0)
    assert block == 40 and (pix[:block, :block] == pix[0, 0]).all()


def test_strength_scales_with_region_size():
    assert blur.blur_sigma(40, 0.6) < blur.blur_sigma(400, 0.6)
    assert blur.blur_sigma(400, 0.0) < blur.blur_sigma(400, 1.0)
    assert blur.blur_sigma(400, 0.6) >= 400 / 8  # research: σ ≥ box/8 defeats plate reading
    assert blur.blur_sigma(2, 0.0) == 1.5 and blur.pixel_block(3, 0.0) == 2


def test_ellipse_leaves_the_corners():
    img = checker()
    out = np.asarray(blur.blur_regions(img, [Region(0, 0, 1, 1, "ellipse")], mode="solid"))
    a = np.asarray(img)
    assert np.array_equal(out[:3, :3], a[:3, :3]) and (out[50, 100] == 0).all()


def test_alpha_is_kept_without_dark_fringes():
    img = Image.new("RGBA", (100, 100), (255, 255, 255, 0))
    ImageDraw.Draw(img).rectangle((40, 0, 59, 99), fill=(255, 255, 255, 255))
    out = blur.blur_regions(img, [Region(0, 0, 1, 1)], strength=1.0)
    arr = np.asarray(out)
    assert out.mode == "RGBA"
    visible = arr[..., 3] > 8
    assert arr[visible][:, :3].min() >= 250  # premultiplied: white stays white where it fades out
    for mode in ("L", "LA", "P", "CMYK", "I;16", "1"):
        src = Image.new(mode, (30, 30))
        assert blur.blur_regions(src, [Region(0.1, 0.1, 0.5, 0.5)]).size == (30, 30)


def test_regions_are_clamped_and_empty_ones_skipped():
    img = checker(50, 50)
    same = blur.blur_regions(img, [Region(0.5, 0.5, 0, 0), Region(1.0, 1.0, 0.5, 0.5)])
    assert np.array_equal(np.asarray(same), np.asarray(img))
    assert Region(0.9, 0.9, 0.5, 0.5).to_pixels(50, 50) == (45, 45, 50, 50)
    assert Region(0.1, 0.1, 0.001, 0.001).to_pixels(50, 50) == (5, 5, 6, 6)  # rounded outwards: ≥ 1 px


def test_argument_validation():
    img = checker(20, 20)
    with pytest.raises(ValueError, match="mode"):
        blur.blur_regions(img, [], mode="smudge")
    with pytest.raises(ValueError, match="strength"):
        blur.blur_regions(img, [], strength=2)
    with pytest.raises(ValueError, match="shape"):
        blur.blur_regions(img, [{"x": 0, "y": 0, "w": 1, "h": 1, "shape": "star"}])
    with pytest.raises(ValueError, match="at most"):
        blur.blur_regions(img, [Region(0, 0, 0.1, 0.1)] * (blur.MAX_REGIONS + 1))


# ------------------------------------------------------------------ the op
def test_op_json_validation():
    op = parse_op({"op": "blur_regions", "regions": [{"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}]})
    assert op.mode == "blur" and op.strength == 0.6 and op.regions[0].shape == "rect"
    bad_regions = [{"x": 1.5, "y": 0, "w": 0.1, "h": 0.1}, {"x": -0.1, "y": 0, "w": 0.1, "h": 0.1},
                   {"x": float("nan"), "y": 0, "w": 0.1, "h": 0.1}, {"x": 0, "y": 0, "w": 0.1},
                   {"x": 0, "y": 0, "w": 0.1, "h": 0.1, "kind": "face"}]
    for region in bad_regions:
        with pytest.raises(ValidationError):
            parse_op({"op": "blur_regions", "regions": [region]})
    for extra in ({"mode": "erase"}, {"strength": 1.5}, {"color": "nope"}, {"regions": "all"}):
        with pytest.raises(ValidationError):
            parse_op({"op": "blur_regions", "regions": [], **extra})
    with pytest.raises(ValidationError):
        parse_op({"op": "blur_regions", "regions": [{"x": 0, "y": 0, "w": 0.1, "h": 0.1}] * (blur.MAX_REGIONS + 1)})


def test_op_in_the_pipeline_and_per_frame():
    src = loaded(checker())
    ops = [{"op": "blur_regions", "regions": [{"x": 0, "y": 0, "w": 0.5, "h": 1}], "mode": "solid",
            "color": "#ff0000"}]
    ctx = pipeline.run(src, ops)
    assert ctx.image.getpixel((10, 10)) == (255, 0, 0) and ctx.image.getpixel((150, 10)) in ((0, 0, 0), (255,) * 3)
    assert pipeline.run(src, [{"op": "blur_regions", "regions": []}]).image is src.image  # nothing to do
    spec = get_op("blur_regions")
    assert spec.frame_apply is not None and not spec.heavy
    frame = spec.frame_apply(Ctx(image=checker(), dpi=None, exif=None, icc_profile=None, source_format="gif"),
                             parse_op(ops[0]), checker(40, 20))
    assert frame.size == (40, 20) and frame.getpixel((5, 5)) == (255, 0, 0)


# ------------------------------------------------------------------ suggestions (detectors faked)
class FakeFace:
    def __init__(self, box, score):
        self.box = tuple(int(v) for v in box)
        self.raw_box = box
        self.score = score


def fake_detectors(monkeypatch, faces=(), plate_dets=(), face_ok=True, plate_ok=True):
    calls = {"face": 0, "plate": 0}

    def detect_faces(img, min_score=0.7, max_side=1024):
        calls["face"] += 1
        return [FakeFace(b, s) for b, s in faces if s >= min_score and b[0] + b[2] <= img.width]

    def detect_plates(img, min_score=0.25, tiles=True, check_cancel=None):
        calls["plate"] += 1
        return [plates.Detection(*b, s) for b, s in plate_dets if s >= min_score]

    monkeypatch.setattr(face, "available", lambda: face_ok)
    monkeypatch.setattr(plates, "available", lambda: plate_ok)
    monkeypatch.setattr(face, "detect", detect_faces)
    monkeypatch.setattr(plates, "detect", detect_plates)
    return calls


def test_suggestions_are_grown_selected_and_sorted(monkeypatch):
    fake_detectors(monkeypatch, faces=[((100, 100, 50, 60), 0.95), ((300, 50, 20, 20), 0.62)],
                   plate_dets=[((500, 400, 80, 20), 0.9), ((10, 10, 40, 10), 0.18)])
    img = Image.new("RGB", (800, 600))
    regions, warnings = blur.suggest_regions(img, ["faces", "plates"])
    assert not warnings
    kinds = [(r.kind, r.selected) for r in regions]
    assert kinds[:2] == [("face", True), ("plate", True)] and ("plate", False) in kinds and ("face", False) in kinds
    f = next(r for r in regions if r.kind == "face" and r.selected)
    assert f.shape == "ellipse" and f.x < 100 / 800 and f.y < 100 / 600 and f.y + f.h > 160 / 600  # grown
    p = next(r for r in regions if r.kind == "plate" and r.selected)
    assert p.shape == "rect" and p.x == pytest.approx((500 - 8) / 800) and p.w == pytest.approx(96 / 800)
    body = json.loads(json.dumps([r.to_dict() for r in regions]))
    assert set(body[0]) == {"kind", "x", "y", "w", "h", "shape", "score", "selected"}
    assert all(0 <= r["x"] <= 1 and 0 <= r["x"] + r["w"] <= 1 + 1e-9 for r in body)


def test_missing_detectors_warn_or_fail(monkeypatch):
    fake_detectors(monkeypatch, faces=[((10, 10, 30, 30), 0.9)], plate_ok=False)
    regions, warnings = blur.suggest_regions(Image.new("RGB", (100, 100)), ["faces", "plates"])
    assert len(regions) == 1 and warnings and "plate" in warnings[0].lower()
    with pytest.raises(ModelUnavailable):
        blur.suggest_regions(Image.new("RGB", (100, 100)), ["plates"])
    with pytest.raises(ValueError, match="level"):
        blur.suggest_regions(Image.new("RGB", (100, 100)), ["faces"], level="max")


# ------------------------------------------------------------------ real detectors
@pytest.mark.skipif(not face.available(), reason="no face detector model installed")
def test_real_face_suggestion_covers_the_astronauts_face():
    img = Image.open(ASTRONAUT).convert("RGB")
    regions, _ = blur.suggest_regions(img, ["faces"])
    best = regions[0]
    assert best.kind == "face" and best.selected and best.shape == "ellipse"
    cx, cy = (best.x + best.w / 2) * 512, (best.y + best.h / 2) * 512
    assert 180 < cx < 280 and 60 < cy < 200  # her face is around (230, 120)


@pytest.mark.skipif(not plates.available(), reason="licence-plate model not installed")
def test_real_plate_suggestion_on_the_car():
    img = Image.open(CAR).convert("RGB")
    regions, _ = blur.suggest_regions(img, ["plates"])
    plate = regions[0]
    assert plate.kind == "plate" and plate.selected
    px = (plate.x * 768, plate.y * 517, plate.w * 768, plate.h * 517)
    assert px[0] <= 151 and px[1] <= 225 and px[0] + px[2] >= 224 and px[1] + px[3] >= 291  # covers the plate


# ------------------------------------------------------------------ GET /api/detect/{id}
@pytest.fixture()
def detect_app(tmp_path, monkeypatch):
    """A minimal app around the router (the app mounts it under /api)."""
    monkeypatch.setattr(img_ai, "_slots", threading.BoundedSemaphore(1))
    app = FastAPI()
    store = FileStore(tmp_path / "s", ttl=600)
    store.init()
    app.state.store = store
    app.include_router(img_ai.router, prefix="/api")
    with TestClient(app) as client:
        yield client, store


def save_image(store: FileStore, img: Image.Image, name: str = "a.png") -> str:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return store.save(name, buf, max_bytes=10**8).id


def test_detect_route_answers_and_caches(detect_app, monkeypatch):
    client, store = detect_app
    calls = fake_detectors(monkeypatch, faces=[((10, 10, 30, 30), 0.9)], plate_dets=[((50, 60, 40, 10), 0.8)])
    file_id = save_image(store, Image.new("RGB", (120, 90)))
    res = client.get(f"/api/detect/{file_id}")
    assert res.status_code == 200 and res.headers["cache-control"] == "no-store"
    body = res.json()
    assert body["width"] == 120 and body["height"] == 90 and body["what"] == ["faces", "plates"]
    assert body["counts"] == {"faces": 1, "plates": 1} and body["unavailable"] == []
    assert {r["kind"] for r in body["regions"]} == {"face", "plate"}
    again = client.get(f"/api/detect/{file_id}?what=plates,faces")
    assert again.status_code == 200 and again.json()["what"] == ["plates", "faces"]
    before = dict(calls)
    assert client.get(f"/api/detect/{file_id}").json() == body  # cached per file + query
    assert calls == before
    only = client.get(f"/api/detect/{file_id}?what=plates&level=low").json()
    assert only["counts"] == {"plates": 1} and all(r["kind"] == "plate" for r in only["regions"])


@pytest.mark.parametrize("query", ["what=cats", "what=", "what=faces,%20,plates,dogs", "level=max",
                                   "what=" + "f" * 50])
def test_detect_route_rejects_bad_queries(detect_app, monkeypatch, query):
    client, store = detect_app
    fake_detectors(monkeypatch)
    file_id = save_image(store, Image.new("RGB", (20, 20)))
    res = client.get(f"/api/detect/{file_id}?{query}")
    assert res.status_code == 422


def test_detect_route_errors(detect_app, monkeypatch):
    client, store = detect_app
    fake_detectors(monkeypatch, face_ok=False, plate_ok=False)
    assert client.get(f"/api/detect/{'0' * 32}").status_code == 404
    assert client.get("/api/detect/..%2F..%2Fetc").status_code in (404, 422)
    assert client.get("/api/detect/XYZ").status_code == 422
    file_id = save_image(store, Image.new("RGB", (20, 20)))
    res = client.get(f"/api/detect/{file_id}")
    assert res.status_code == 503 and "unavailable" in res.json()["detail"]
    store.set_info(file_id, kind="pdf")
    assert client.get(f"/api/detect/{file_id}").status_code == 422
    junk = store.save("x.png", io.BytesIO(b"not an image at all"), max_bytes=1000).id
    fake_detectors(monkeypatch)
    assert client.get(f"/api/detect/{junk}").status_code in (415, 422)


def test_detect_route_partial_results_are_not_cached(detect_app, monkeypatch):
    client, store = detect_app
    calls = fake_detectors(monkeypatch, faces=[((10, 10, 30, 30), 0.9)], plate_ok=False)
    file_id = save_image(store, Image.new("RGB", (120, 90)))
    body = client.get(f"/api/detect/{file_id}").json()
    assert body["unavailable"] == ["plates"] and body["warnings"]
    client.get(f"/api/detect/{file_id}")
    assert calls["face"] == 2  # recomputed: the plate model may be installed later


def test_detect_route_is_busy_instead_of_queueing(detect_app, monkeypatch):
    client, store = detect_app
    fake_detectors(monkeypatch, faces=[((10, 10, 30, 30), 0.9)])
    monkeypatch.setattr(img_ai, "SLOT_WAIT_S", 0.05)
    file_id = save_image(store, Image.new("RGB", (40, 40)))
    assert img_ai._slots.acquire(timeout=1)
    try:
        res = client.get(f"/api/detect/{file_id}")
    finally:
        img_ai._slots.release()
    assert res.status_code == 503 and res.headers.get("retry-after") == "5"


@pytest.mark.skipif(not (face.available() and plates.available()), reason="detector model not installed")
def test_detect_route_with_real_models(detect_app):
    client, store = detect_app
    file_id = save_image(store, Image.open(CAR).convert("RGB"), "car.png")
    body = client.get(f"/api/detect/{file_id}?what=faces,plates").json()
    assert body["counts"]["plates"] >= 1 and body["counts"]["faces"] == 0
    region = next(r for r in body["regions"] if r["kind"] == "plate")
    op = {"op": "blur_regions", "regions": [{k: region[k] for k in ("x", "y", "w", "h", "shape")}]}
    assert parse_op(op).regions[0].shape == "rect"  # the answer feeds the op as is


def test_features_never_raise(monkeypatch):
    monkeypatch.setattr(plates, "available", lambda: (_ for _ in ()).throw(OSError("disk")))
    feats = img_ai.features()
    assert feats["plates"] is False and set(feats) == {"upscale", "matting", "plates", "faces", "person_segmentation"}


# ------------------------------------------------------------------ job tools (real job runner)
@pytest.fixture()
def jobs(tmp_path):
    from app.core.jobs import JobManager

    settings = Settings(tmp_dir=str(tmp_path / "heyp"), max_jobs=2)
    store = FileStore(settings.tmp_dir, settings.ttl_seconds)
    store.init()
    manager = JobManager(settings, store)
    manager.start()
    yield manager, store
    manager.stop()


def finish(manager, job, timeout: float = 120.0):
    deadline = time.monotonic() + timeout
    while not job.finished_flag and time.monotonic() < deadline:
        time.sleep(0.05)
    assert job.finished_flag, "job did not finish"
    return job.to_json()


def test_tools_are_registered_with_lanes_and_limits():
    from app.core import registry

    up, rb, bl = (registry.get(i) for i in ("img.upscale", "img.remove_background", "img.blur"))
    assert (up.lane, rb.lane, bl.lane) == ("heavy", "heavy", "light")
    assert up.inputs == rb.inputs == bl.inputs == ("image",)
    assert rb.limits["memory_mb"] * 1024 * 1024 >= matting.PEAK_BYTES
    schema = registry.options_schema(bl)
    assert schema["properties"]["detect"]["default"] == ["faces"]
    with pytest.raises(Exception):
        registry.parse_options(bl, {"detect": [], "regions": []})  # nothing to blur
    with pytest.raises(Exception):
        registry.parse_options(up, {"factor": 3})


def test_blur_job_blurs_detected_faces(jobs, monkeypatch):
    manager, store = jobs
    fake_detectors(monkeypatch, faces=[((0, 0, 100, 100), 0.95)])
    a = save_image(store, checker(200, 100), "one.png")
    b = save_image(store, checker(200, 100), "two.png")
    job = manager.submit("img.blur", [a, b], {"detect": ["faces"], "mode": "solid", "color": "#00ff00",
                                               "format": "png"})
    body = finish(manager, job)
    assert body["status"] == "done", body
    files = body["result"]["files"]
    assert [f["name"] for f in files] == ["one_blur.png", "two_blur.png"]
    out = Image.open(store.get(files[0]["id"]).path)
    assert out.getpixel((40, 40)) == (0, 255, 0) and out.getpixel((190, 90)) in ((0, 0, 0), (255, 255, 255))
    assert body["result"]["data"]["images"][0]["found"] == {"faces": 1}


def test_blur_job_with_nothing_found_warns(jobs, monkeypatch):
    manager, store = jobs
    fake_detectors(monkeypatch)
    a = save_image(store, checker(60, 40))
    body = finish(manager, manager.submit("img.blur", [a], {"detect": ["faces", "plates"]}))
    assert body["status"] == "done" and any("Nothing to blur" in w for w in body["warnings"])


def test_jobs_report_unavailable_models(jobs, monkeypatch):
    manager, store = jobs
    monkeypatch.setattr(upscale, "available", lambda model="balanced": False)
    a = save_image(store, checker(40, 40))
    from app.core.errors import ToolError

    with pytest.raises(ToolError) as info:
        manager.submit("img.upscale", [a], {})
    assert info.value.status == 503


def test_remove_background_job(jobs, monkeypatch):
    manager, store = jobs
    monkeypatch.setattr(matting, "available", lambda name=None: True)
    monkeypatch.setattr(segment, "available", lambda: True)

    def left_half(img, **_kw):
        m = np.zeros((img.height, img.width), np.float32)
        m[:, : img.width // 2] = 1
        return m

    monkeypatch.setattr(matting, "object_mask", left_half)
    a = save_image(store, Image.new("RGB", (80, 40), (200, 30, 30)), "cat.jpg")
    body = finish(manager, manager.submit("img.remove_background", [a], {}))
    assert body["status"] == "done", body
    f = body["result"]["files"][0]
    assert f["name"] == "cat_remove_background.png"
    out = Image.open(store.get(f["id"]).path)
    assert out.mode == "RGBA" and out.getpixel((70, 20))[3] == 0 and out.getpixel((10, 20))[3] == 255
    body = finish(manager, manager.submit("img.remove_background", [a], {"format": "jpeg"}))
    assert body["status"] == "done" and any("JPG can't be transparent" in w for w in body["warnings"])


@pytest.mark.skipif(not upscale.available("balanced"), reason="upscale model not installed")
def test_upscale_job_end_to_end_with_progress(jobs):
    manager, store = jobs
    a = save_image(store, Image.new("RGB", (300, 120), (20, 90, 160)), "small.png")
    job = manager.submit("img.upscale", [a], {"factor": 2})
    body = finish(manager, job)
    assert body["status"] == "done", body
    f = body["result"]["files"][0]
    assert f["name"] == "small_upscale.png" and (f.get("width"), f.get("height")) in ((600, 240), (None, None))
    assert Image.open(store.get(f["id"]).path).size == (600, 240)
    progress = [e for e in job.events if e[0] == "progress"]
    assert progress and any("Upscaling 2×" in e[1]["message"] for e in progress)


def test_upscale_job_limits_are_413(jobs, monkeypatch):
    manager, store = jobs
    monkeypatch.setattr(upscale, "available", lambda model="balanced": True)
    monkeypatch.setenv("HEYP_UPSCALE_MAX_INPUT_MP", "0.001")
    a = save_image(store, Image.new("RGB", (100, 100)))
    body = finish(manager, manager.submit("img.upscale", [a], {"factor": 4}))
    assert body["status"] == "failed" and body["error"]["status"] == 413
    assert "Upscaling works on images up to" in body["error"]["detail"]
