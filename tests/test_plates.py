"""Tests for licence-plate suggestions (app.imaging.plates, YOLOv9-t-640 end-to-end ONNX)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.imaging import ortsession, plates
from app.imaging.plates import Detection

FIXTURES = Path(__file__).parent / "fixtures"
CAR = FIXTURES / "ai" / "car_plate.jpg"  # CC0, one plate at about (150, 224, 74×67) in 768×517
PLATE = (150.6, 224.4, 74.0, 67.4)

needs_model = pytest.mark.skipif(not plates.available(), reason="licence-plate model not installed")


def box_iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    iw = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    ih = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    inter = max(0.0, iw) * max(0.0, ih)
    return inter / (a[2] * a[3] + b[2] * b[3] - inter)


@pytest.fixture(scope="module")
def car() -> Image.Image:
    if not CAR.is_file():
        pytest.skip("tests/fixtures/ai/car_plate.jpg missing")
    return Image.open(CAR).convert("RGB")


# ------------------------------------------------------------------ geometry helpers (no model)
@pytest.mark.parametrize("n", [1, 1000, 1280, 1281, 1600, 2560, 5120, 12345])
def test_tile_origins_cover_with_overlap(n):
    starts = plates.tile_origins(n)
    assert starts[0] == 0 and starts == sorted(set(starts))
    assert min(n, starts[-1] + plates.TILE) == n  # the last tile reaches the far edge
    for a, b in zip(starts, starts[1:]):
        assert b - a <= plates.TILE * (1 - plates.TILE_OVERLAP) + 1  # ≥ 20 % overlap, no gaps


def test_merge_unites_overlapping_boxes_and_keeps_the_best_score():
    a = Detection(10, 10, 100, 40, 0.9)
    b = Detection(40, 15, 100, 40, 0.4)   # overlaps a by > 50 % of its own area
    c = Detection(400, 400, 50, 20, 0.6)  # far away
    d = Detection(500, 10, 10, 10, 0.2)
    merged = plates.merge([d, b, c, a])
    assert [m.score for m in merged] == [0.9, 0.6, 0.2]
    assert (merged[0].x, merged[0].y, merged[0].x2, merged[0].y2) == (10, 10, 140, 55)
    assert plates.merge([]) == []


def test_to_dict_is_json_safe():
    d = Detection(np.float32(1.25), np.float64(2.0), 3.0, 4.0, np.float32(0.5))  # type: ignore[arg-type]
    body = json.loads(json.dumps(d.to_dict()))
    assert body == {"kind": "plate", "box": [1.2, 2.0, 3.0, 4.0], "score": 0.5}


def test_argument_checks_and_tiny_images():
    with pytest.raises(TypeError):
        plates.detect(np.zeros((10, 10, 3)))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        plates.detect(Image.new("RGB", (20, 20)), min_score=1.5)
    assert plates.detect(Image.new("RGB", (7, 300))) == []  # too small to hold a plate: no model needed


def test_missing_model_raises_model_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(plates, "_session", ortsession.ModelSession(plates.MODEL_FILE, label="licence-plate"))
    monkeypatch.setenv("HEYP_MODELS_DIR", str(tmp_path))
    assert not plates.available()
    with pytest.raises(ortsession.ModelUnavailable, match="licence-plate"):
        plates.detect(Image.new("RGB", (64, 64)))


# ------------------------------------------------------------------ model-backed
@needs_model
def test_finds_the_plate(car):
    dets = plates.detect(car)
    assert dets and dets[0].score >= 0.5
    assert box_iou((dets[0].x, dets[0].y, dets[0].w, dets[0].h), PLATE) >= 0.6
    assert all(d.score >= plates.MIN_SCORE for d in dets)
    assert json.dumps([d.to_dict() for d in dets])


@needs_model
def test_finds_the_plate_in_large_images_with_tiles(car):
    big = car.resize((3072, 2068), Image.Resampling.LANCZOS)  # 4×: whole pass + 1280-px tiles
    dets = plates.detect(big)
    assert dets
    k = 3072 / 768
    expected = tuple(v * k for v in PLATE)
    best = max(box_iou((d.x, d.y, d.w, d.h), expected) for d in dets)
    assert best >= 0.5
    covering = [d for d in dets if box_iou((d.x, d.y, d.w, d.h), expected) >= 0.5][0]
    # merged boxes cover the plate (they may grow, they never shrink below it)
    assert covering.x <= expected[0] + 0.1 * expected[2] and covering.x2 >= expected[0] + 0.9 * expected[2]


@needs_model
@pytest.mark.parametrize("mode", ["RGBA", "L", "P", "CMYK"])
def test_other_modes_work(car, mode):
    img = car.convert(mode)
    dets = plates.detect(img, tiles=False)
    if mode in ("RGBA", "P", "CMYK"):
        assert dets and box_iou((dets[0].x, dets[0].y, dets[0].w, dets[0].h), PLATE) >= 0.5
    else:
        assert isinstance(dets, list)  # grey: may or may not be found, must not crash


@needs_model
def test_no_plates_in_a_portrait_or_noise():
    astronaut = Image.open(FIXTURES / "astronaut.png").convert("RGB")
    assert plates.detect(astronaut) == []
    rng = np.random.default_rng(0)
    assert plates.detect(Image.fromarray(rng.integers(0, 256, (900, 1600, 3), dtype=np.uint8))) == []


@needs_model
def test_cancel_is_checked_between_tiles(car):
    big = car.resize((3072, 2068))
    calls = []

    class Stop(Exception):
        pass

    def cancel():
        calls.append(1)
        raise Stop()

    with pytest.raises(Stop):
        plates.detect(big, check_cancel=cancel)
    assert calls == [1]
