"""Tests for app.imaging.passport (framing geometry, passport photos, print sheets)."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.imaging import face as face_mod
from app.imaging import ops, passport, presets, segment
from app.imaging.face import Face
from app.imaging.passport import Analysis

ASTRONAUT = Path(__file__).parent / "fixtures" / "astronaut.png"
needs_models = pytest.mark.skipif(not (face_mod.available() and segment.available()),
                                  reason="face/segmentation models not installed")

US = presets.get_preset("us")
EYE_PRESETS = [pid for pid in ("us", "schengen") if pid in presets.PRESETS]
INFO_KEYS = {"preset", "width", "height", "dpi", "face_found", "head_ratio", "eye_from_bottom", "lines", "bands",
             "background_edit_allowed", "crown_includes_hair", "roll_deg", "levelled_deg", "face_box_out", "warnings"}
# Ground truth of tests/fixtures/astronaut.png, read off zoomed crops: top of the hair and chin rows.
TRUE_HAIR, TRUE_CHIN = 15.5, 177.0
TRUE_HEAD = TRUE_CHIN - TRUE_HAIR
BACKDROP = (90, 140, 90)


def _fake_face(x: int = 400, y: int = 300, w: int = 200, h: int = 250, landmarks: bool = True) -> Face:
    lm = {}
    if landmarks:
        ey = y + 0.36 * h
        lm = {"right_eye": (x + 0.25 * w, ey), "left_eye": (x + 0.75 * w, ey), "nose": (x + 0.5 * w, y + 0.56 * h),
              "mouth_right": (x + 0.3 * w, y + 0.76 * h), "mouth_left": (x + 0.7 * w, y + 0.76 * h)}
    return Face(box=(x, y, w, h), score=0.99, landmarks=lm)


def _head_mask(size: tuple[int, int], center: tuple[float, float], axes: tuple[float, float]) -> np.ndarray:
    """Person mask: ellipse head plus a body block below the head centre."""
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = center
    ax, ay = axes
    m = (((xx - cx) / ax) ** 2 + ((yy - cy) / ay) ** 2 <= 1.0).astype(np.float32)
    m[int(cy):, int(cx - 2 * ax):int(cx + 2 * ax)] = 1.0
    return m


def _two_tone(size, mask, person=(200, 150, 120), backdrop=(90, 140, 90)) -> Image.Image:
    arr = np.where(mask[..., None] > 0.5, np.array(person, np.uint8), np.array(backdrop, np.uint8))
    return Image.fromarray(arr.astype(np.uint8))


# --- head geometry (synthetic face, no models) ------------------------------------------------


def test_head_geometry_estimate_with_landmarks():
    f = _fake_face()
    g = passport.head_geometry(f)
    assert g["chin_y"] == 550
    assert g["eye_y"] == pytest.approx(390)
    assert g["center_x"] == pytest.approx(500)
    assert g["crown_y"] == pytest.approx(390 - (550 - 390) - 25)  # eye − (chin − eye) − 0.10·h
    assert g["head_h"] == pytest.approx(g["chin_y"] - g["crown_y"])
    assert g["crown_source"] == "estimate"


def test_head_geometry_without_landmarks():
    g = passport.head_geometry(_fake_face(landmarks=False))
    assert g["eye_y"] == pytest.approx(300 + 0.38 * 250)
    assert g["center_x"] == pytest.approx(500)


def test_head_geometry_crown_from_mask():
    f = _fake_face()
    mask = _head_mask((1000, 1200), (500, 400), (130, 230))  # head top at y = 170
    g = passport.head_geometry(f, mask)
    assert g["crown_source"] == "mask"
    assert g["crown_y"] == pytest.approx(172, abs=3)
    # Same mask at half resolution, mapped through image_size.
    half = mask[::2, ::2]
    g2 = passport.head_geometry(f, half, image_size=(1000, 1200))
    assert g2["crown_y"] == pytest.approx(172, abs=4)


def test_head_geometry_mask_fallbacks():
    f = _fake_face()
    empty = np.zeros((1200, 1000), np.float32)
    assert passport.head_geometry(f, empty)["crown_source"] == "estimate"
    # A mask running far above the head (e.g. raised arm) is clamped to a plausible head height.
    full = np.ones((1200, 1000), np.float32)
    g = passport.head_geometry(f, full)
    est_h = 550 - (390 - 160 - 25)
    assert g["head_h"] == pytest.approx(1.5 * est_h)


def test_head_geometry_crown_cut_by_top_edge():
    """VIS-1: hair running into row 0 is not the crown; the geometric estimate (above the image) is used."""
    f = _fake_face(y=60)  # eye 150, chin 310 → estimated crown 150 − 160 − 25 = −35
    mask = _head_mask((1000, 1200), (500, 160), (130, 200))  # head top at −40: cut by the photo
    g = passport.head_geometry(f, mask, image_size=(1000, 1200))
    assert g["crown_source"] == "clipped" and g["crown_clipped"] is True
    assert g["crown_y"] == pytest.approx(-35, abs=0.5)  # not 0
    # Same head with room above: the mask crown is used and nothing is clipped.
    g2 = passport.head_geometry(_fake_face(y=360), _head_mask((1000, 1200), (500, 460), (130, 200)))
    assert g2["crown_source"] == "mask" and g2["crown_clipped"] is False


def test_head_geometry_chin_cut_by_bottom_edge():
    """VIS-6: a detector box running out of the bottom of the photo → chin extrapolated from the mouth."""
    f = _fake_face()  # eye 390, mouth 490, box bottom 550
    cut = Face(box=(400, 300, 200, 230), score=0.99, landmarks=f.landmarks, raw_box=(400, 300, 200, 230))
    g = passport.head_geometry(cut, image_size=(1000, 530))
    assert g["chin_clipped"] is True
    assert g["chin_y"] == pytest.approx(490 + passport.CHIN_FROM_MOUTH * 100)
    # The unclamped detector box is the chin when it is known (no extrapolation needed).
    raw = Face(box=(400, 300, 200, 230), score=0.99, landmarks=f.landmarks, raw_box=(400.0, 300.0, 200.0, 252.0))
    assert raw.bottom == 552.0
    assert passport.head_geometry(raw, image_size=(1000, 600))["chin_y"] == pytest.approx(552)
    assert passport.head_geometry(f, image_size=(1000, 1200))["chin_clipped"] is False


def test_skull_estimate_is_calibrated_and_hair_aware():
    """crown_includes_hair=False: skull top ≈ eye − SKULL_RATIO·(chin − eye), never above the visible top."""
    f = _fake_face()  # eye 390, chin 550
    below = 160.0
    big_hair = _head_mask((1000, 1200), (500, 300), (150, 200))  # visible top ≈ 101: lots of hair
    g = passport.head_geometry(f, big_hair, crown_includes_hair=False)
    assert g["crown_y"] == g["skull_y"] == pytest.approx(390 - passport.SKULL_RATIO * below, abs=0.5)
    assert g["hair_y"] == pytest.approx(101, abs=3) and g["head_h"] == pytest.approx(550 - g["skull_y"])
    # Adult norms (vertex→pupil ≈ 1.0–1.07 × pupil→menton) bound the estimate.
    assert 1.0 <= (390 - g["skull_y"]) / below <= 1.07
    # Bald/close-cropped: the visible top is below the prior → the estimate follows it, never above it.
    bald = _head_mask((1000, 1200), (500, 450), (110, 210))  # visible top ≈ 242: 0.93 × eye→chin
    gb = passport.head_geometry(f, bald, crown_includes_hair=False)
    assert gb["skull_y"] >= gb["hair_y"]
    assert gb["skull_y"] - gb["hair_y"] < 0.05 * below
    # Including the hair, the crown is the visible top.
    assert passport.head_geometry(f, big_hair)["crown_y"] == g["hair_y"]
    # The estimate rises monotonically with the visible top and saturates at the prior.
    tops = [passport._skull_height(below, t) for t in (120.0, 150.0, 165.0, 180.0, 250.0)]
    assert tops == sorted(tops) and tops[0] == pytest.approx(120, abs=3)
    assert tops[-1] == pytest.approx(passport.SKULL_RATIO * below, rel=1e-3)
    assert all(h <= t for h, t in zip(tops, (120.0, 150.0, 165.0, 180.0, 250.0)))


# --- framing (synthetic analysis, no models) ----------------------------------------------------


@pytest.fixture
def scene():
    size = (1000, 1200)
    mask = _head_mask(size, (500, 400), (130, 200))  # crown ≈ 202: eye→crown ≈ 54 % of the head
    img = _two_tone(size, mask)
    return img, Analysis(faces=[_fake_face()], mask=mask, size=size)


def test_make_us_exact_geometry(scene):
    img, analysis = scene
    out, info = passport.make(img, US, analysis=analysis)
    assert out.size == (600, 600) and out.mode == "RGB"
    assert out.info["dpi"] == (300, 300) and info["dpi"] == 300
    assert info["face_found"] is True
    assert info["head_ratio"] == pytest.approx((US.head_min + US.head_max) / 2, abs=0.002)
    assert info["eye_from_bottom"] == pytest.approx((US.eye_min + US.eye_max) / 2, abs=0.002)
    fbo = info["face_box_out"]
    assert fbo["center_x"] == pytest.approx(300, abs=0.5)
    assert fbo["crown_y"] < fbo["eye_y"] < fbo["chin_y"]
    assert (fbo["chin_y"] - fbo["crown_y"]) / 600 == pytest.approx(info["head_ratio"], abs=0.002)
    # Stable info keys (documented in make()); v1.1 adds the guide data the preview needs (C2–C4).
    assert set(info) == INFO_KEYS
    assert (info["preset"], info["width"], info["height"]) == ("us", 600, 600)
    assert info["lines"] == {"crown": fbo["crown_y"], "chin": fbo["chin_y"], "eye": fbo["eye_y"]}
    assert info["lines"]["eye"] == pytest.approx(600 * (1 - info["eye_from_bottom"]), abs=0.1)
    assert info["bands"] == {"head_min": US.head_min, "head_max": US.head_max, "eye_min": US.eye_min,
                             "eye_max": US.eye_max, "top_min": None, "top_max": None}
    assert info["background_edit_allowed"] is True and info["crown_includes_hair"] is True
    assert info["roll_deg"] is None and info["levelled_deg"] == 0  # flat synthetic eyes: not measurable
    # Background replaced (white corners), subject kept.
    assert out.getpixel((3, 3)) == (255, 255, 255)
    assert out.getpixel((300, round(fbo["eye_y"]))) == pytest.approx((200, 150, 120), abs=2)


def test_make_keeps_background_when_not_replacing(scene):
    img, analysis = scene
    out, info = passport.make(img, US, analysis=analysis, replace_background=False)
    assert out.getpixel((3, 3)) == pytest.approx((90, 140, 90), abs=2)
    assert not any("beyond" in w for w in info["warnings"])


def test_make_no_mask_warns_and_keeps_background(scene):
    img, analysis = scene
    out, info = passport.make(img, US, analysis=Analysis(analysis.faces, None, analysis.size))
    assert any("unavailable" in w for w in info["warnings"])
    assert out.getpixel((3, 3)) == pytest.approx((90, 140, 90), abs=2)


def test_make_crown_placement_without_eye_range(scene):
    img, analysis = scene
    p = presets.custom_preset(35, 45, head_min=0.7, head_max=0.8)
    out, info = passport.make(img, p, analysis=analysis)
    ow, oh = p.output_size()
    assert out.size == (ow, oh)
    target_h = oh * 0.75
    assert info["face_box_out"]["crown_y"] == pytest.approx((oh - target_h) * 0.45, abs=0.5)
    assert info["eye_from_bottom"] is not None


def test_make_top_range_placement(scene):
    img, analysis = scene
    p = replace(presets.custom_preset(35, 45, head_min=0.6, head_max=0.7), top_min=0.08, top_max=0.12)
    _, info = passport.make(img, p, analysis=analysis)
    oh = p.output_size()[1]
    assert info["face_box_out"]["crown_y"] == pytest.approx(0.10 * oh, abs=0.5)


def test_eye_line_lowered_within_range_to_keep_crown(scene):
    """Head 75 % with eyes 50–70 % from the bottom: the mid eye line would crop the crown."""
    img, analysis = scene
    p = presets.custom_preset(76.2, 101.6, head_min=0.75, head_max=0.75, eye_min=0.5, eye_max=0.7)
    _, info = passport.make(img, p, analysis=analysis)
    oh = p.output_size()[1]
    assert 0.5 <= info["eye_from_bottom"] < 0.6
    assert info["face_box_out"]["crown_y"] == pytest.approx(passport.MIN_TOP_MARGIN * oh, abs=0.5)
    assert not any("cut off" in w for w in info["warnings"])


def test_zoom_and_offsets_change_framing(scene):
    img, analysis = scene
    _, base = passport.make(img, US, analysis=analysis)
    _, zoomed = passport.make(img, US, analysis=analysis, zoom=1.1)
    assert zoomed["head_ratio"] == pytest.approx(base["head_ratio"] * 1.1, abs=0.002)
    assert any("Head height" in w for w in zoomed["warnings"]) == (zoomed["head_ratio"] > US.head_max + 0.005)
    _, moved = passport.make(img, US, analysis=analysis, offset_x=0.1, offset_y=0.1)
    assert moved["face_box_out"]["center_x"] == pytest.approx(base["face_box_out"]["center_x"] + 60, abs=0.5)
    assert moved["face_box_out"]["eye_y"] == pytest.approx(base["face_box_out"]["eye_y"] + 60, abs=0.5)
    assert any("Eye line" in w for w in moved["warnings"])


def test_frame_beyond_source_is_filled(scene):
    img, _ = scene
    f = _fake_face(y=20)
    out, info = passport.make(img, US, analysis=Analysis([f], None, img.size),
                              replace_background=False, background="#ff0000")
    assert any("beyond" in w for w in info["warnings"])
    assert out.getpixel((300, 0)) == (255, 0, 0)


def test_low_resolution_multiple_faces_and_bad_index():
    img = Image.new("RGB", (300, 360), (128, 128, 128))
    faces = [_fake_face(110, 90, 80, 100), _fake_face(10, 10, 30, 40)]
    _, info = passport.make(img, US, analysis=Analysis(faces, None, img.size), face_index=5,
                            replace_background=False)
    text = " ".join(info["warnings"])
    assert "Low resolution" in text and "2 faces detected" in text and "not found" in text


def test_face_index_selects_face():
    img = Image.new("RGB", (2000, 1200), (128, 128, 128))
    faces = [_fake_face(300, 300, 200, 250), _fake_face(1300, 400, 180, 220)]
    _, a = passport.make(img, US, analysis=Analysis(faces, None, img.size), replace_background=False)
    _, b = passport.make(img, US, analysis=Analysis(faces, None, img.size), face_index=1,
                         replace_background=False)
    assert a["face_box_out"]["center_x"] == pytest.approx(300, abs=0.5)
    assert b["face_box_out"]["center_x"] == pytest.approx(300, abs=0.5)
    assert b["head_ratio"] == pytest.approx(a["head_ratio"], abs=0.002)


def test_no_face_falls_back_to_centred_crop():
    grad = np.tile(np.linspace(0, 255, 800, dtype=np.float32), (600, 1))
    img = Image.fromarray(np.dstack([grad] * 3).astype(np.uint8))
    out, info = passport.make(img, US, analysis=Analysis([], None, img.size))
    assert out.size == (600, 600)
    assert info["face_found"] is False and info["head_ratio"] is None and info["face_box_out"] is None
    assert any("No face" in w for w in info["warnings"])
    # Centred 600×600 window of the 800×600 source starts at x = 100.
    assert out.getpixel((0, 300))[0] == pytest.approx(100 * 255 / 799, abs=3)
    assert out.getpixel((599, 300))[0] == pytest.approx(699 * 255 / 799, abs=3)


def test_missing_detector_is_not_reported_as_no_face(monkeypatch):
    """Without any face detector the warning says so, rather than blaming the photo."""
    monkeypatch.setattr(face_mod, "available", lambda: False)
    img = Image.new("RGB", (800, 600), (200, 200, 200))
    _, info = passport.make(img, US, analysis=Analysis([], None, img.size))
    assert info["face_found"] is False
    assert any(w.startswith("Face detection is unavailable") for w in info["warnings"])
    assert not any("No face" in w for w in info["warnings"])


def test_transparent_background(scene):
    img, analysis = scene
    out, _ = passport.make(img, US, analysis=analysis, background="transparent")
    assert out.mode == "RGBA"
    assert out.getpixel((3, 3))[3] == 0
    assert out.getpixel((300, 300))[3] == 255


def test_rgba_source_is_flattened(scene):
    img, analysis = scene
    out, _ = passport.make(img.convert("RGBA"), US, analysis=analysis)
    assert out.mode == "RGB"


def test_stale_analysis_is_recomputed(monkeypatch, scene):
    img, analysis = scene
    calls = []
    monkeypatch.setattr(passport, "analyze", lambda im, **kw: calls.append(im.size) or Analysis([], None, im.size))
    small = img.resize((500, 600))
    _, info = passport.make(small, US, analysis=analysis)
    assert calls == [(500, 600)] and info["face_found"] is False


@pytest.mark.parametrize("kwargs", [
    {"zoom": 0}, {"zoom": 10}, {"offset_x": 2}, {"offset_y": -1.5}, {"face_index": -1},
    {"background": "not-a-colour"},
])
def test_make_rejects_bad_arguments(scene, kwargs):
    img, analysis = scene
    with pytest.raises(ValueError):
        passport.make(img, US, analysis=analysis, **kwargs)


def test_make_accepts_preset_id(scene):
    img, analysis = scene
    out, _ = passport.make(img, "us", analysis=analysis)
    assert out.size == US.output_size()
    with pytest.raises(ValueError, match="unknown passport preset"):
        passport.make(img, "atlantis", analysis=analysis)


def test_make_skull_presets_measure_without_hair(scene):
    """C3: crown_includes_hair=False → head_ratio is chin→skull; the hair top sits above that crown line."""
    img, analysis = scene
    p = replace(presets.custom_preset(35, 45, head_min=0.7, head_max=0.8), crown_includes_hair=False)
    _, info = passport.make(img, p, analysis=analysis)
    assert info["crown_includes_hair"] is False
    assert info["head_ratio"] == pytest.approx(0.75, abs=0.002)
    assert info["face_box_out"]["top_y"] < info["lines"]["crown"]
    _, hair = passport.make(img, replace(p, crown_includes_hair=True), analysis=analysis)
    assert hair["face_box_out"]["top_y"] == pytest.approx(hair["lines"]["crown"])


def test_background_edit_not_allowed_warns_only_when_replacing(scene):
    img, analysis = scene
    uk = presets.get_preset("uk")
    _, info = passport.make(img, uk, analysis=analysis)
    msg = "United Kingdom rules don't allow digitally altered backgrounds — photograph against a plain light wall instead."
    assert msg in info["warnings"] and info["background_edit_allowed"] is False
    _, kept = passport.make(img, uk, analysis=analysis, replace_background=False)
    assert not any("altered" in w for w in kept["warnings"])
    _, us = passport.make(img, US, analysis=analysis)
    assert not any("altered" in w for w in us["warnings"])


def _rotate_point(p, center, deg):
    """Where PIL's Image.rotate(deg, center=…) (counter-clockwise) moves point `p`."""
    c, s = np.cos(np.radians(deg)), np.sin(np.radians(deg))
    dx, dy = p[0] - center[0], p[1] - center[1]
    return center[0] + c * dx + s * dy, center[1] - s * dx + c * dy


def _tilted_scene(img, analysis, deg):
    """The scene rotated counter-clockwise by `deg` about the eye midpoint, with matching eyes and mask."""
    f = analysis.faces[0]
    center = f.eye_center()
    rot = img.rotate(deg, Image.Resampling.BICUBIC, center=center, fillcolor=BACKDROP)
    mask = Image.fromarray(analysis.mask).rotate(deg, Image.Resampling.BILINEAR, center=center)
    lm = {k: _rotate_point(v, center, deg) for k, v in f.landmarks.items()}
    eyes = (lm["right_eye"], lm["left_eye"])
    return rot, Analysis([Face(f.box, f.score, lm)], np.asarray(mask, np.float32), rot.size, eyes=[eyes])


def _person(out: Image.Image) -> np.ndarray:
    return (np.abs(np.asarray(out, np.int16) - (200, 150, 120)).max(axis=2) < 40)


def test_level_rotates_about_the_eyes_in_one_resample(scene):
    """C4: a 10° roll is measured from the pupils and levelled; the result matches the upright framing."""
    img, analysis = scene
    upright, ref = passport.make(img, US, analysis=analysis)
    rot, tilted = _tilted_scene(img, analysis, 10)
    out, info = passport.make(rot, US, analysis=tilted)
    assert info["roll_deg"] == pytest.approx(-10, abs=0.05) and info["levelled_deg"] == pytest.approx(-10, abs=0.05)
    assert not any("tilted" in w for w in info["warnings"])
    a, b = _person(upright), _person(out)
    assert (a & b).sum() / (a | b).sum() > 0.97  # same framing as the upright head
    assert info["face_box_out"]["eye_y"] == pytest.approx(ref["face_box_out"]["eye_y"], abs=1)
    assert info["head_ratio"] == pytest.approx(ref["head_ratio"], abs=0.002)
    # level=False keeps the tilt (and warns above TILT_WARN_DEG).
    still, info_off = passport.make(rot, US, analysis=tilted, level=False)
    assert info_off["levelled_deg"] == 0 and info_off["roll_deg"] == pytest.approx(-10, abs=0.05)
    assert any("tilted by about 10°" in w for w in info_off["warnings"])
    c = _person(still)
    assert (a & c).sum() / (a | c).sum() < 0.95


@pytest.mark.parametrize(("roll", "level", "levelled", "warn"), [
    (0.5, True, 0.0, None),            # below LEVEL_MIN_DEG: left alone
    (5.0, True, 5.0, None),
    (14.0, True, 14.0, None),
    (20.0, True, 0.0, "too much to level"),
    (-20.0, True, 0.0, "too much to level"),
    (5.0, False, 0.0, None),           # within the ICAO tolerance
    (12.0, False, 0.0, "turn on levelling"),
])
def test_level_thresholds(scene, roll, level, levelled, warn):
    img, analysis = scene
    f = analysis.faces[0]
    ex, ey = f.eye_center()
    dx, dy = 50 * np.cos(np.radians(roll)), 50 * np.sin(np.radians(roll))
    eyes = ((ex - dx, ey - dy), (ex + dx, ey + dy))
    a = Analysis(analysis.faces, analysis.mask, analysis.size, eyes=[eyes])
    _, info = passport.make(img, US, analysis=a, level=level)
    assert info["roll_deg"] == pytest.approx(roll, abs=0.05)
    assert info["levelled_deg"] == pytest.approx(levelled, abs=0.05)
    tilt = [w for w in info["warnings"] if "tilted" in w]
    assert (warn is None and not tilt) or (warn is not None and len(tilt) == 1 and warn in tilt[0])


def test_unmeasured_roll_falls_back_to_a_coarse_warning(scene):
    img, analysis = scene
    f = analysis.faces[0]
    lm = dict(f.landmarks, right_eye=(450.0, 385.0), left_eye=(550.0, 395.0))  # YuNet-style 5.7°
    a = Analysis([Face(f.box, f.score, lm)], analysis.mask, analysis.size, eyes=[None])
    _, info = passport.make(img, US, analysis=a)
    assert info["roll_deg"] is None and info["levelled_deg"] == 0
    assert any("looks tilted" in w for w in info["warnings"])
    _, flat = passport.make(img, US, analysis=Analysis(analysis.faces, analysis.mask, analysis.size, eyes=[None]))
    assert not any("tilted" in w for w in flat["warnings"])


def test_replaced_background_hides_fill_unless_the_person_is_cut(scene):
    """VIS-7: with a replaced background the out-of-photo fill is invisible, so only a cut person warns."""
    img, analysis = scene
    f = _fake_face(y=60)  # frame reaches above the photo; the head (top ≈ 0 → clipped) is cut there
    mask = _head_mask(img.size, (500, 160), (130, 200))
    _, cut = passport.make(_two_tone(img.size, mask), US, analysis=Analysis([f], mask, img.size))
    assert any("top of the head is cut off in the source" in w for w in cut["warnings"])
    # Head well inside the photo, frame still beyond the top edge: nothing to see, no warning.
    f2 = _fake_face(y=110)  # eye 200, chin 360
    mask2 = _head_mask(img.size, (500, 220), (130, 200))  # top at 20: a gap above the hair
    out, ok = passport.make(_two_tone(img.size, mask2), US, analysis=Analysis([f2], mask2, img.size))
    assert not any("beyond" in w or "cuts off" in w or "cut off" in w for w in ok["warnings"])
    _, kept = passport.make(_two_tone(img.size, mask2), US, analysis=Analysis([f2], mask2, img.size),
                            replace_background=False)
    assert any("beyond" in w for w in kept["warnings"])


def test_side_clipping_warning(scene):
    """VIS-3: hair wider than the frame is reported with the side(s) it is cut on."""
    img, analysis = scene
    wide = analysis.mask.copy()
    wide[150:560, 120:880] = 1.0  # very wide hair from the crown to the chin
    _, info = passport.make(_two_tone(img.size, wide), US, analysis=Analysis(analysis.faces, wide, img.size))
    assert any("wider than the photo frame" in w and "left and right" in w for w in info["warnings"])
    _, ok = passport.make(img, US, analysis=analysis)
    assert not any("wider" in w for w in ok["warnings"])


# --- real photo (models) ------------------------------------------------------------------


@pytest.fixture(scope="module")
def astronaut() -> Image.Image:
    if not ASTRONAUT.is_file():
        pytest.skip("tests/fixtures/astronaut.png missing")
    return Image.open(ASTRONAUT).convert("RGB")


@pytest.fixture(scope="module")
def astro_analysis(astronaut) -> Analysis:
    if not (face_mod.available() and segment.available()):
        pytest.skip("face/segmentation models not installed")
    return passport.analyze(astronaut)


@needs_models
def test_analyze_astronaut_matches_ground_truth(astronaut, astro_analysis):
    """VIS-12: crown, chin and head height against the fixture's measured hair top and chin."""
    a = astro_analysis
    assert len(a.faces) == 1 and a.size == astronaut.size
    assert a.mask is not None and a.mask.shape == (512, 512)
    g = passport.head_geometry(a.faces[0], a.mask, image_size=a.size)
    assert g["crown_source"] == "mask" and not g["crown_clipped"] and not g["chin_clipped"]
    assert g["crown_y"] == pytest.approx(TRUE_HAIR, abs=3)
    assert g["chin_y"] == pytest.approx(TRUE_CHIN, abs=3)
    assert g["head_h"] == pytest.approx(TRUE_HEAD, rel=0.03)
    # Natural head roll of the fixture: pupils at ≈(203, 101) and (246, 103.5) → ≈3.3°.
    assert face_mod.roll_deg(a.eyes[0]) == pytest.approx(3.3, abs=1.0)
    # Skull top (crown_includes_hair=False): below the hair, within the adult proportion band.
    s = passport.head_geometry(a.faces[0], a.mask, image_size=a.size, crown_includes_hair=False)
    assert g["crown_y"] + 3 < s["crown_y"] < g["crown_y"] + 20
    assert 1.0 <= (s["eye_y"] - s["crown_y"]) / (s["chin_y"] - s["eye_y"]) <= 1.07


def _measure_output(out: Image.Image) -> dict:
    """Independent measurements on a passport photo with a white background (pixels + re-detection)."""
    found = face_mod.detect(out)
    assert len(found) == 1
    f = found[0]
    x, _, w, _ = f.box
    arr = np.asarray(out.convert("RGB"), np.int16)
    person = (255 - arr).max(axis=2) > 40
    cols = person[:, x:x + w].mean(axis=1) >= 0.15
    eyes = face_mod.refine_eyes(out, f) or (f.landmarks["right_eye"], f.landmarks["left_eye"])
    return {"top": int(np.argmax(cols)), "chin": f.bottom, "eye": (eyes[0][1] + eyes[1][1]) / 2,
            "center_x": (eyes[0][0] + eyes[1][0]) / 2, "roll": face_mod.roll_deg(eyes)}


@needs_models
@pytest.mark.parametrize("preset_id", EYE_PRESETS)
def test_make_real_photo_meets_spec(astronaut, astro_analysis, preset_id):
    p = presets.get_preset(preset_id)
    out, info = passport.make(astronaut, p, analysis=astro_analysis, background="#ffffff")
    assert out.size == p.output_size()
    assert info["face_found"] and info["levelled_deg"] == pytest.approx(info["roll_deg"])
    assert p.head_min <= info["head_ratio"] <= p.head_max
    if p.eye_min is not None and p.eye_max is not None:
        assert p.eye_min <= info["eye_from_bottom"] <= p.eye_max
    # Measured on the output pixels, not the self-reported numbers.
    m = _measure_output(out)
    oh, ow = out.height, out.width
    assert m["eye"] == pytest.approx(info["lines"]["eye"], abs=0.03 * oh)
    assert m["center_x"] == pytest.approx(info["face_box_out"]["center_x"], abs=0.03 * ow)
    assert m["top"] == pytest.approx(info["face_box_out"]["top_y"], abs=0.02 * oh)
    assert m["chin"] == pytest.approx(info["lines"]["chin"], abs=0.025 * oh)
    assert abs(m["roll"]) < 1.5  # levelled
    hair_ratio = (m["chin"] - m["top"]) / oh
    if p.crown_includes_hair:
        assert p.head_min - 0.015 <= hair_ratio <= p.head_max + 0.015
        assert hair_ratio == pytest.approx(info["head_ratio"], abs=0.025)
    else:  # chin→skull is in spec, so the head with hair is taller than the reported ratio
        assert hair_ratio > info["head_ratio"] + 0.01
    if p.eye_min is not None:
        assert p.eye_min - 0.015 <= 1 - m["eye"] / oh <= p.eye_max + 0.015


@needs_models
def test_real_photo_warnings_are_specific(astronaut, astro_analysis):
    """VIS-7: a good portrait gets no 'beyond the photo' noise; VIS-3: 'in' (82.5 % head) cuts the hair."""
    for pid in ("us", "au", "jp", "schengen", "uk", "nl"):
        _, info = passport.make(astronaut, pid, analysis=astro_analysis)
        assert not any("beyond" in w or "cuts off" in w or "cut off" in w for w in info["warnings"]), pid
    for pid in ("us", "au", "jp"):
        _, info = passport.make(astronaut, pid, analysis=astro_analysis)
        assert not any("wider than the photo frame" in w for w in info["warnings"]), pid
    _, info = passport.make(astronaut, "in", analysis=astro_analysis)
    assert any("wider than the photo frame" in w and "right" in w for w in info["warnings"])


@needs_models
@pytest.mark.parametrize("cut", [35, 50])
def test_real_crown_cut_by_source_top(astronaut, cut):
    """VIS-1: hair cut by the top of the source → warned, and the head is not under-measured."""
    img = astronaut.crop((0, cut, 512, 512))
    a = passport.analyze(img)
    g = passport.head_geometry(a.faces[0], a.mask, image_size=img.size)
    assert g["crown_clipped"] and g["crown_source"] == "clipped"
    assert abs(g["head_h"] - TRUE_HEAD) / TRUE_HEAD < 0.08
    _, info = passport.make(img, "schengen", analysis=a)
    assert any("top of the head is cut off in the source photo" in w for w in info["warnings"])
    _, full = passport.make(astronaut, "schengen")
    assert not any("in the source photo" in w for w in full["warnings"])


@needs_models
@pytest.mark.parametrize("height", [170, 160])
def test_real_chin_cut_by_source_bottom(astronaut, height):
    """VIS-6: chin cut by the bottom of the source → extrapolated from the mouth, and warned."""
    img = astronaut.crop((0, 0, 512, height))
    a = passport.analyze(img)
    g = passport.head_geometry(a.faces[0], a.mask, image_size=img.size)
    assert g["chin_clipped"] and abs(g["head_h"] - TRUE_HEAD) / TRUE_HEAD < 0.05
    _, info = passport.make(img, "us", analysis=a)
    assert any("chin is cut off in the source photo" in w for w in info["warnings"])


@needs_models
def test_real_tilted_head_is_levelled(astronaut):
    """C4/VIS-2: an 8° rotation (≈11° roll) is measured from the pupils and levelled."""
    img = ops.rotate(astronaut, 8, expand=True, fill="#c8c8c8")
    out, info = passport.make(img, "us", background="#ffffff")
    assert info["roll_deg"] == pytest.approx(11.3, abs=1.5)
    assert info["levelled_deg"] == info["roll_deg"]
    assert not any("tilted" in w for w in info["warnings"])
    m = _measure_output(out)
    assert abs(m["roll"]) < 1.5
    assert (m["chin"] - m["top"]) / 600 == pytest.approx(info["head_ratio"], abs=0.03)
    # Without levelling the tilt stays and is reported; the fixture's own 3° is within tolerance.
    _, off = passport.make(img, "us", level=False)
    assert off["levelled_deg"] == 0 and any("tilted by about 11°" in w for w in off["warnings"])
    _, upright = passport.make(astronaut, "us", level=False)
    assert not any("tilted" in w for w in upright["warnings"])
    # Beyond LEVEL_MAX_DEG the photo is left alone with a warning.
    steep = ops.rotate(astronaut, -20, expand=True, fill="#c8c8c8")
    _, info = passport.make(steep, "us")
    assert info["levelled_deg"] == 0 and any("too much to level" in w for w in info["warnings"])


@needs_models
def test_replace_background_false_keeps_original_pixels(astronaut, astro_analysis):
    replaced, _ = passport.make(astronaut, US, analysis=astro_analysis, background="#ffffff")
    kept, info = passport.make(astronaut, US, analysis=astro_analysis, replace_background=False)
    v0, u0 = 60, 0  # a 30×30 patch of backdrop left of the head (inside the source after levelling)
    corner_r = np.asarray(replaced)[v0:v0 + 30, u0:u0 + 30].reshape(-1, 3).mean(axis=0)
    corner_k = np.asarray(kept)[v0:v0 + 30, u0:u0 + 30].reshape(-1, 3).mean(axis=0)
    assert corner_r == pytest.approx([255, 255, 255], abs=2)
    assert np.abs(corner_k - 255).max() > 30  # the grey studio backdrop, not white
    # The kept patch matches the source backdrop at the mapped location. The frame is levelled
    # (rotated by levelled_deg about the eye midpoint), so map the patch through that rotation.
    face = astro_analysis.faces[0]
    theta = np.radians(info["levelled_deg"])
    g = passport.head_geometry(face, astro_analysis.mask, image_size=astronaut.size, roll=info["levelled_deg"])
    s = (info["lines"]["chin"] - info["lines"]["crown"]) / g["head_h"]
    fbo = info["face_box_out"]
    qx, qy = (u0 + 15 - fbo["center_x"]) / s, (v0 + 15 - fbo["eye_y"]) / s  # patch centre, levelled, from the pivot
    px = g["center_x"] + np.cos(theta) * qx - np.sin(theta) * qy
    py = g["eye_y"] + np.sin(theta) * qx + np.cos(theta) * qy
    half = 15 / s
    src = np.asarray(astronaut.crop((round(px - half), round(py - half), round(px + half), round(py + half))))
    assert corner_k == pytest.approx(src.reshape(-1, 3).mean(axis=0), abs=2)  # ignoring the rotation: off by 4
    # The face itself is untouched by the background replacement.
    fy, fx = round(fbo["eye_y"]) + 40, round(fbo["center_x"])
    patch = (slice(fy - 20, fy + 20), slice(fx - 20, fx + 20))
    diff = np.abs(np.asarray(replaced, np.int16)[patch] - np.asarray(kept, np.int16)[patch])
    assert diff.mean() < 1.0


@needs_models
def test_analyze_is_not_cached_by_key_and_handles_large_images(astronaut):
    """VIS-10: no module-level cache — a flipped image gets its own (mirrored) analysis."""
    a = passport.analyze(astronaut)
    flipped = passport.analyze(astronaut.transpose(Image.Transpose.FLIP_LEFT_RIGHT))
    assert flipped is not a
    x, _, w, _ = a.faces[0].box
    assert flipped.faces[0].box[0] == pytest.approx(512 - x - w, abs=4)
    big = astronaut.resize((2400, 2400), Image.Resampling.LANCZOS)
    a = passport.analyze(big)
    assert a.mask.shape == (passport.ANALYSIS_MASK_MAX, passport.ANALYSIS_MASK_MAX)
    out, info = passport.make(big, US, analysis=a)
    assert out.size == (600, 600)
    assert not any("Low resolution" in w for w in info["warnings"])


@needs_models
def test_small_face_in_large_photo_is_redetected(astronaut):
    """VIS-5: a face only ~13 px tall at detection scale is re-detected on a crop (chin within 5 %)."""
    canvas = Image.new("RGB", (8000, 6000), (200, 200, 205))
    ox, oy = 4000 - 256, 3000 - 256
    canvas.paste(astronaut, (ox, oy))
    coarse = face_mod.detect(canvas)[0]
    a = passport.analyze(canvas)
    g = passport.head_geometry(a.faces[0], a.mask, image_size=canvas.size)
    assert abs(g["head_h"] - TRUE_HEAD) / TRUE_HEAD < 0.05
    assert g["chin_y"] - oy == pytest.approx(TRUE_CHIN, abs=5)
    assert abs(coarse.bottom - oy - TRUE_CHIN) > abs(g["chin_y"] - oy - TRUE_CHIN)

@needs_models
def test_real_no_face_image():
    img = Image.new("RGB", (640, 480), (230, 225, 220))
    ImageDraw.Draw(img).rectangle((200, 150, 440, 330), fill=(40, 60, 90))
    out, info = passport.make(img, US)
    assert out.size == (600, 600) and info["face_found"] is False


# --- print sheets ---------------------------------------------------------------------------


def _photo(w: int, h: int, color=(200, 30, 30)) -> Image.Image:
    return Image.new("RGB", (w, h), color)


def test_sheet_2x2_on_4x6_gives_6():
    out, info = passport.sheet(_photo(600, 600), paper="4x6", src_dpi=300)
    assert info["copies"] == 6 and (info["cols"], info["rows"]) == (2, 3)
    assert info["orientation"] == "portrait" and info["gap_mm"] == 0
    assert out.size == (1200, 1800) and out.info["dpi"] == (300, 300)
    assert info["photo_px"] == [600, 600]
    assert out.getpixel((300, 300)) == (200, 30, 30)


def test_sheet_35x45_on_4x6_gives_8():
    out, info = passport.sheet(_photo(413, 531), paper="4x6", photo_width_mm=35, photo_height_mm=45)
    assert info["copies"] == 8 and (info["cols"], info["rows"]) == (4, 2)
    assert info["orientation"] == "landscape"
    assert (info["gap_mm"], info["margin_mm"]) == (2, 0)
    assert out.size == (1800, 1200)


def test_sheet_35x45_on_a4_gives_32():
    out, info = passport.sheet(_photo(413, 531), paper="a4", photo_width_mm=35, photo_height_mm=45)
    assert info["copies"] == 32 and (info["cols"], info["rows"]) == (8, 4)
    assert info["orientation"] == "landscape"
    assert (info["gap_mm"], info["margin_mm"]) == (0, 4)
    assert out.size == (3508, 2480)


def test_sheet_photo_size_from_pixels_and_dpi():
    # Schengen output (413×531 px @300 dpi) derives ≈35×45 mm.
    _, info = passport.sheet(_photo(413, 531), paper="4x6", src_dpi=(300, 300))
    assert info["copies"] == 8 and info["photo_px"] == [413, 531]
    _, info600 = passport.sheet(_photo(600, 600), paper="4x6", src_dpi=600)  # 1×1 in
    assert info600["photo_mm"] == [25.4, 25.4]


def test_sheet_fewer_copies_keep_preferred_spacing():
    out, info = passport.sheet(_photo(413, 531), paper="4x6", photo_width_mm=35, photo_height_mm=45, copies=4)
    assert info["copies"] == 4
    assert (info["gap_mm"], info["margin_mm"]) == (2, 4)
    reds = (np.asarray(out) == (200, 30, 30)).all(axis=2)
    assert reds.sum() == pytest.approx(4 * 413 * 531, rel=0.02)


def test_sheet_too_many_copies_warns_and_clamps():
    _, info = passport.sheet(_photo(600, 600), paper="4x6", src_dpi=300, copies=10)
    assert info["copies"] == 6
    assert any("Only 6 copies" in w for w in info["warnings"])


def test_sheet_dpi_and_orientation():
    out, info = passport.sheet(_photo(600, 600), paper="4x6", src_dpi=300, dpi=600)
    assert out.size == (2400, 3600) and info["photo_px"] == [1200, 1200]
    _, forced = passport.sheet(_photo(413, 531), paper="4x6", orientation="portrait",
                               photo_width_mm=35, photo_height_mm=45)
    assert forced["orientation"] == "portrait" and forced["copies"] == 6


def test_sheet_cut_lines():
    grey = passport.CUT_LINE_COLOR
    with_lines, info = passport.sheet(_photo(413, 531), paper="4x6", photo_width_mm=35, photo_height_mm=45)
    arr = np.asarray(with_lines)
    assert (arr == grey).all(axis=2).any()
    gap_px = round(2 / 25.4 * 300)
    x0 = (1800 - (4 * 413 + 3 * gap_px)) // 2
    x_line = x0 + 413 + gap_px // 2
    assert tuple(arr[600, x_line]) == grey  # vertical line through the first column gap
    without, _ = passport.sheet(_photo(413, 531), paper="4x6", photo_width_mm=35, photo_height_mm=45,
                                cut_lines=False)
    assert not (np.asarray(without) == grey).all(axis=2).any()
    # gap 0 → lines on shared edges
    shared, _ = passport.sheet(_photo(600, 600), paper="4x6", src_dpi=300)
    assert tuple(np.asarray(shared)[100, 600]) == grey


def test_sheet_aspect_mismatch_and_alpha():
    rgba = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    out, info = passport.sheet(rgba, paper="4x6", photo_width_mm=35, photo_height_mm=45, cut_lines=False)
    assert any("centre-cropped" in w for w in info["warnings"])
    assert (np.asarray(out) == 255).all()  # transparent photo flattened onto white paper


@pytest.mark.parametrize("kwargs", [
    {"paper": "b9"}, {"orientation": "sideways"}, {"dpi": 10}, {"gap_mm": -1}, {"copies": -2},
    {"photo_width_mm": 0}, {"photo_width_mm": 500, "photo_height_mm": 500}, {"copies": 201},
    {"photo_width_mm": 4, "photo_height_mm": 45},
])
def test_sheet_rejects_bad_arguments(kwargs):
    with pytest.raises(ValueError):
        passport.sheet(_photo(600, 600), **kwargs)


def test_sheet_dpi_none_follows_the_source():
    """C5: dpi=None prints at the source (preset) dpi clamped to 300–600, else 300."""
    nl = presets.get_preset("nl")  # 35×45 mm at 400 dpi = 551×709 px
    out, info = passport.sheet(_photo(*nl.output_size()), paper="4x6", src_dpi=400)
    assert info["dpi"] == 400 and out.info["dpi"] == (400, 400) and info["photo_px"] == [551, 709]
    assert info["photo_mm"] == pytest.approx([35, 45], abs=0.1) and out.size == (2400, 1600)
    assert passport.sheet(_photo(600, 600), paper="4x6")[1]["dpi"] == 300
    assert [passport.sheet_dpi(d) for d in (None, 0, 72, (400, 400), 457.2, 1000)] == [300, 300, 300, 400, 457, 600]
    assert passport.sheet(_photo(600, 600), paper="4x6", src_dpi=400, dpi=300)[1]["dpi"] == 300


def test_sheet_gaps_per_axis():
    """VIS-8: the count is maximised first, then each axis gets as much of the gap as it has room for."""
    india = presets.get_preset("in")  # 630×810 px at 457 dpi ≈ 35×45 mm
    out, info = passport.sheet(_photo(*india.output_size()), paper="13x18", src_dpi=india.dpi)
    assert info["copies"] == 10 and (info["cols"], info["rows"]) == (5, 2)
    assert info["gap_mm"] == 0 and info["gap_y_mm"] == 2 and 0 <= info["gap_x_mm"] < 2
    # The two rows no longer touch: a white gutter of ≈2 mm between them.
    reds = (np.asarray(out) == (200, 30, 30)).all(axis=2)
    rows_with_red = np.flatnonzero(reds.any(axis=1))
    gaps = np.diff(rows_with_red)
    assert gaps.max() - 1 == pytest.approx(2 / 25.4 * info["dpi"], abs=2)
    _, four = passport.sheet(_photo(413, 531), paper="4x6", photo_width_mm=35, photo_height_mm=45)
    assert (four["gap_mm"], four["margin_mm"]) == (2, 0)
    assert (four["gap_x_mm"], four["gap_y_mm"]) == (2, 2)


def test_sheet_bounded_work(monkeypatch):
    """API-1: tiny photos are rejected, copies=0 is capped and tiles are pasted per row strip."""
    with pytest.raises(ValueError, match="too small"):
        passport.sheet(_photo(64, 48), paper="a4", photo_width_mm=0.001, photo_height_mm=0.001)
    with pytest.raises(ValueError, match="too small"):
        passport.sheet(_photo(600, 600), paper="a4", src_dpi=100000)  # size from the image's own dpi
    calls = []
    paste = Image.Image.paste
    monkeypatch.setattr(Image.Image, "paste", lambda self, *a, **kw: calls.append(1) or paste(self, *a, **kw))
    out, info = passport.sheet(_photo(60, 60), paper="a4", photo_width_mm=5, photo_height_mm=5, cut_lines=False)
    assert info["copies"] == passport.MAX_COPIES
    assert any(f"maximum of {passport.MAX_COPIES}" in w for w in info["warnings"])
    assert len(calls) <= info["cols"] + info["rows"] + 1
    reds = (np.asarray(out) == (200, 30, 30)).all(axis=2)
    assert reds.sum() == passport.MAX_COPIES * info["photo_px"][0] * info["photo_px"][1]


def test_sheet_partial_last_row():
    out, info = passport.sheet(_photo(413, 531), paper="4x6", photo_width_mm=35, photo_height_mm=45, copies=7,
                               cut_lines=False)
    assert info["copies"] == 7 and (info["cols"], info["rows"]) == (4, 2)
    reds = (np.asarray(out) == (200, 30, 30)).all(axis=2)
    rows = np.flatnonzero(reds.any(axis=1))
    split = rows[np.argmax(np.diff(rows))] + 1  # first row of the white gutter between the two rows
    assert reds[:split].sum() == 4 * 413 * 531 and reds[split:].sum() == 3 * 413 * 531
