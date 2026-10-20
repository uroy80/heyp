"""Tests for app.imaging.face (YuNet face detection)."""
from __future__ import annotations

from pathlib import Path

import math

import pytest
from PIL import Image

from app.imaging import face, ops

ASTRONAUT = Path(__file__).parent / "fixtures" / "astronaut.png"
# Pupil centres of the fixture read off a zoomed crop (subject's right eye, left eye) and its roll.
PUPILS = ((203.0, 101.0), (246.0, 103.5))
BASE_ROLL = math.degrees(math.atan2(2.5, 43.0))

needs_detector = pytest.mark.skipif(not face.available(), reason="no face detector model installed")


@pytest.fixture(scope="module")
def astronaut() -> Image.Image:
    if not ASTRONAUT.is_file():
        pytest.skip("tests/fixtures/astronaut.png missing")
    return Image.open(ASTRONAUT).convert("RGB")


def _inside(pt: tuple[float, float], box: tuple[int, int, int, int]) -> bool:
    x, y, w, h = box
    return x <= pt[0] <= x + w and y <= pt[1] <= y + h


@needs_detector
def test_backend_is_yunet():
    assert face.backend() == "yunet"


@needs_detector
def test_astronaut_has_exactly_one_face_with_sane_landmarks(astronaut):
    faces = face.detect(astronaut)
    assert len(faces) == 1
    f = faces[0]
    x, y, w, h = f.box
    assert f.score > 0.8
    # Head of the astronaut sits in the upper-middle of the 512×512 image.
    assert 150 < x < 210 and 40 < y < 90 and 70 < w < 120 and 90 < h < 140
    lm = f.landmarks
    assert set(lm) == set(face.LANDMARK_NAMES)
    assert all(_inside(p, f.box) for p in lm.values())
    # "right"/"left" are the subject's sides: the right eye appears on the image's left.
    assert lm["right_eye"][0] < lm["nose"][0] < lm["left_eye"][0]
    assert lm["mouth_right"][0] < lm["mouth_left"][0]
    eye_y = (lm["right_eye"][1] + lm["left_eye"][1]) / 2
    mouth_y = (lm["mouth_right"][1] + lm["mouth_left"][1]) / 2
    assert eye_y < lm["nose"][1] < mouth_y
    assert abs(lm["right_eye"][1] - lm["left_eye"][1]) < 0.1 * h  # upright face
    assert f.eye_center() == pytest.approx(((lm["right_eye"][0] + lm["left_eye"][0]) / 2, eye_y))


@needs_detector
def test_detection_on_large_image_is_scaled_back(astronaut):
    small = face.detect(astronaut)[0]
    big_img = astronaut.resize((1536, 1536), Image.Resampling.LANCZOS)  # > 1024 → detected on a copy
    big = face.detect(big_img)
    assert len(big) == 1
    for a, b in zip(big[0].box, small.box):
        assert a == pytest.approx(3 * b, abs=0.04 * 1536)
    for name, (px, py) in big[0].landmarks.items():
        sx, sy = small.landmarks[name]
        assert px == pytest.approx(3 * sx, abs=12) and py == pytest.approx(3 * sy, abs=12)


@needs_detector
def test_faces_sorted_by_area(astronaut):
    canvas = Image.new("RGB", (1100, 600), (128, 128, 128))
    canvas.paste(astronaut.resize((300, 300)), (20, 150))
    canvas.paste(astronaut, (560, 40))
    faces = face.detect(canvas)
    assert len(faces) == 2
    assert faces[0].area > faces[1].area
    assert faces[0].box[0] > 560 and faces[1].box[0] < 320


@needs_detector
@pytest.mark.parametrize("side", [512, 1600])  # 1600 exercises the downscale-first path
@pytest.mark.parametrize("mode", ["RGBA", "L", "LA", "P"])
def test_other_modes(astronaut, mode, side):
    img = astronaut if side == 512 else astronaut.resize((side, side), Image.Resampling.BICUBIC)
    faces = face.detect(img.convert(mode))
    assert len(faces) == 1


@needs_detector
def test_no_face_in_blank_or_tiny_image():
    assert face.detect(Image.new("RGB", (640, 480), (200, 190, 180))) == []
    assert face.detect(Image.new("RGB", (8, 8))) == []


def test_to_dict_shape():
    f = face.Face(box=(10, 20, 30, 40), score=0.912345,
                  landmarks={"right_eye": (15.04, 30.0), "left_eye": (30.0, 30.0)})
    d = f.to_dict()
    assert d == {"box": [10, 20, 30, 40], "score": 0.9123,
                 "landmarks": {"right_eye": [15.0, 30.0], "left_eye": [30.0, 30.0]}}
    assert face.Face(box=(0, 0, 5, 5), score=0.5).eye_center() is None


def test_invalid_arguments():
    with pytest.raises(TypeError):
        face.detect("not an image")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        face.detect(Image.new("RGB", (64, 64)), min_score=1.5)


def test_missing_model_degrades_gracefully(monkeypatch, tmp_path):
    monkeypatch.setenv("HEYP_MODELS_DIR", str(tmp_path))
    monkeypatch.setattr(face, "_yunet", None)
    monkeypatch.setattr(face, "_yunet_failed", False)
    monkeypatch.setattr(face, "_haar", None)
    monkeypatch.setattr(face, "_haar_failed", False)
    assert face.backend() in (None, "haar")  # OpenCV 5 wheels ship no Haar cascades
    if face.backend() is None:
        assert face.available() is False
        assert face.detect(Image.new("RGB", (200, 200))) == []


@needs_detector
def test_unclamped_box_is_kept_when_the_face_leaves_the_image(astronaut):
    """VIS-6: the clamped box stops at the image edge; raw_box/bottom keep the detector's chin."""
    full = face.detect(astronaut)[0]
    assert full.raw_box is not None and full.bottom == pytest.approx(full.box[1] + full.box[3], abs=1)
    cut = face.detect(astronaut.crop((0, 0, 512, 165)))[0]
    assert cut.box[1] + cut.box[3] == 165
    assert cut.bottom > 166  # the detector saw the chin continue below the edge
    assert face.Face(box=(1, 2, 3, 4), score=0.9).bottom == 6.0


@needs_detector
def test_refine_eyes_finds_the_pupils(astronaut):
    f = face.detect(astronaut)[0]
    eyes = face.refine_eyes(astronaut, f)
    assert eyes is not None
    for got, want in zip(eyes, PUPILS):
        assert got == pytest.approx(want, abs=2.0)
    assert face.roll_deg(eyes) == pytest.approx(BASE_ROLL, abs=1.0)


@needs_detector
@pytest.mark.parametrize("angle", [4, 8, -8, 12, -12])
def test_roll_from_pupils_tracks_rotation(astronaut, angle):
    """C4/VIS-2: YuNet's eye landmarks stay nearly level on a tilted head; the refined pupils don't."""
    img = ops.rotate(astronaut, angle, expand=True, fill="#c8c8c8")  # clockwise = positive roll
    f = face.detect(img)[0]
    true = BASE_ROLL + angle
    assert face.roll_deg(face.refine_eyes(img, f)) == pytest.approx(true, abs=1.5)
    if abs(angle) >= 8:
        yunet = face.roll_deg((f.landmarks["right_eye"], f.landmarks["left_eye"]))
        assert abs(yunet - true) > 4  # why raw landmarks are never used for levelling


def test_refine_eyes_rejects_unmeasurable_faces():
    flat = Image.new("RGB", (400, 400), (180, 160, 150))
    lm = {"right_eye": (170.0, 180.0), "left_eye": (230.0, 180.0)}
    assert face.refine_eyes(flat, face.Face((140, 120, 120, 150), 0.9, lm)) is None  # no dark pupil
    assert face.refine_eyes(flat, face.Face((140, 120, 120, 150), 0.9)) is None      # no landmarks
    assert face.roll_deg(None) is None
    assert face.roll_deg(((0.0, 0.0), (10.0, 10.0))) == pytest.approx(45.0)


def test_refine_eyes_on_synthetic_pupils():
    from PIL import ImageDraw

    img = Image.new("RGB", (400, 400), (200, 170, 150))
    draw = ImageDraw.Draw(img)
    pupils = ((168.0, 178.0), (232.0, 190.0))  # 10.6° roll
    for x, y in pupils:
        draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=(30, 25, 20))
    lm = {"right_eye": (171.0, 183.0), "left_eye": (229.0, 185.0)}  # upright-biased landmarks
    eyes = face.refine_eyes(img, face.Face((140, 120, 120, 150), 0.9, lm))
    assert eyes is not None
    for got, want in zip(eyes, pupils):
        assert got == pytest.approx(want, abs=1.0)
    assert face.roll_deg(eyes) == pytest.approx(math.degrees(math.atan2(12, 64)), abs=1.0)


@needs_detector
def test_refine_small_redetects_tiny_faces(astronaut):
    """VIS-5: a face ~13 px tall at detection scale gets its box (chin) from a second pass on a crop."""
    canvas = Image.new("RGB", (8000, 6000), (200, 200, 205))
    ox, oy = 4000 - 256, 3000 - 256
    canvas.paste(astronaut, (ox, oy))
    coarse = face.detect(canvas)
    assert len(coarse) == 1 and coarse[0].box[3] * face.DETECT_MAX_SIDE / 8000 < face.REFINE_BELOW
    fine = face.refine_small(canvas, coarse)
    ref = face.detect(astronaut)[0]
    assert fine[0].bottom - oy == pytest.approx(ref.bottom, abs=4)
    assert abs(fine[0].bottom - oy - ref.bottom) < abs(coarse[0].bottom - oy - ref.bottom)
    for name, (x, y) in fine[0].landmarks.items():
        assert (x - ox, y - oy) == pytest.approx(ref.landmarks[name], abs=6)
    # Faces that are large enough, or have no landmarks, are returned unchanged.
    assert face.refine_small(astronaut, [ref])[0] is ref
    plain = face.Face((10, 10, 20, 20), 0.75)
    assert face.refine_small(canvas, [plain])[0] is plain
