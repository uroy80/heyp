"""Sanity checks for the researched passport/visa preset table."""
from __future__ import annotations

import re

import pytest

from app.imaging.presets import (MM_PER_INCH, PAPERS, PRESETS, PassportPreset, custom_preset, get_preset,
                                 list_presets)

ALL = list(PRESETS.values())
IDS = [p.id for p in ALL]

# Rough face proportion used for feasibility checks: the eye line sits ~45 % of the
# chin→crown height below the crown (hair included).
EYE_BELOW_CROWN = 0.45


def _mid(lo: float, hi: float) -> float:
    return (lo + hi) / 2


def test_minimum_table_size_and_required_ids() -> None:
    assert len(ALL) >= 18
    for required in ("us", "schengen"):
        assert required in PRESETS


def test_us_first_then_sorted_by_name() -> None:
    assert IDS[0] == "us"
    names = [p.name.casefold() for p in ALL[1:]]
    assert names == sorted(names)


def test_ids_unique_and_well_formed() -> None:
    assert len(IDS) == len(set(IDS))
    for pid in IDS:
        assert re.fullmatch(r"[a-z]{2,8}(-[a-z]+)?", pid), pid
        assert pid != "custom"


def test_dict_keys_match_ids() -> None:
    for key, preset in PRESETS.items():
        assert isinstance(preset, PassportPreset)
        assert key == preset.id
        assert get_preset(key) is preset


@pytest.mark.parametrize("p", ALL, ids=IDS)
def test_metadata(p: PassportPreset) -> None:
    assert p.name.strip() and "—" in p.name
    assert p.country == "EU" or re.fullmatch(r"[A-Z]{2}", p.country)
    assert p.source.startswith("https://")
    assert p.notes.strip()
    assert re.fullmatch(r"#[0-9a-f]{6}", p.background), p.background


@pytest.mark.parametrize("p", ALL, ids=IDS)
def test_sizes_positive(p: PassportPreset) -> None:
    assert p.width_mm > 0 and p.height_mm > 0
    assert 72 <= p.dpi <= 1200
    assert (p.pixel_width is None) == (p.pixel_height is None)
    if p.pixel_width is not None:
        assert p.pixel_width > 0 and p.pixel_height > 0


@pytest.mark.parametrize("p", ALL, ids=IDS)
def test_output_size(p: PassportPreset) -> None:
    w, h = p.output_size()
    if p.pixel_width:
        assert (w, h) == (p.pixel_width, p.pixel_height)
    else:
        assert (w, h) == (round(p.width_mm / MM_PER_INCH * p.dpi), round(p.height_mm / MM_PER_INCH * p.dpi))
    # Pixel specs must agree with mm × dpi so print sheets reproduce the physical size.
    assert abs(w - p.width_mm * p.dpi / MM_PER_INCH) <= 1
    assert abs(h - p.height_mm * p.dpi / MM_PER_INCH) <= 1
    d = p.to_dict()
    assert (d["width_px"], d["height_px"]) == (w, h)


@pytest.mark.parametrize("p", ALL, ids=IDS)
def test_head_range(p: PassportPreset) -> None:
    assert 0 < p.head_min <= p.head_max < 1


@pytest.mark.parametrize("p", ALL, ids=IDS)
def test_eye_range(p: PassportPreset) -> None:
    assert (p.eye_min is None) == (p.eye_max is None)
    if p.eye_min is None:
        return
    assert 0 < p.eye_min <= p.eye_max < 1
    # Crown implied by a mid-range eye line must stay inside the frame, chin above the bottom edge.
    head = _mid(p.head_min, p.head_max)
    crown = _mid(p.eye_min, p.eye_max) + EYE_BELOW_CROWN * head
    assert crown < 1
    assert crown - head > 0


@pytest.mark.parametrize("p", ALL, ids=IDS)
def test_top_range(p: PassportPreset) -> None:
    assert (p.top_min is None) == (p.top_max is None)
    if p.top_min is None:
        return
    assert 0 <= p.top_min <= p.top_max < 1
    # The smallest head under the smallest margin must leave the chin inside the photo.
    assert p.top_min + p.head_min < 1
    # Mid-range placement leaves room below the chin.
    assert 1 - _mid(p.top_min, p.top_max) - _mid(p.head_min, p.head_max) > 0


def test_eye_below_crown_when_both_given() -> None:
    for p in ALL:
        if p.eye_min is None or p.top_min is None:
            continue
        crown_from_bottom = 1 - _mid(p.top_min, p.top_max)
        assert _mid(p.eye_min, p.eye_max) < crown_from_bottom, p.id


def test_known_values() -> None:
    us = get_preset("us")
    assert us.output_size() == (600, 600)
    assert us.head_min == pytest.approx(0.5) and us.head_max == pytest.approx(0.6875)
    assert get_preset("schengen").output_size() == (413, 531)
    assert get_preset("uk").head_min * 45 == pytest.approx(29)
    assert get_preset("ca").output_size() == (591, 827)
    assert get_preset("cn").top_max * 48 == pytest.approx(5)
    assert get_preset("in").output_size() == (630, 810)
    assert get_preset("fr").background != "#ffffff"  # white is forbidden in France


# Official figures re-checked against each authority (mm on the printed photo, or fractions for
# digital-only specs). (width_mm, height_mm, head_lo, head_hi) with heads in mm unless noted.
OFFICIAL_MM = {
    "au": (35, 45, 32, 36), "ca": (50, 70, 31, 36), "ca-visa": (35, 45, 31, 36), "cn": (33, 48, 28, 33),
    "fr": (35, 45, 32, 36), "jp": (35, 45, 32, 36), "my": (35, 50, 25, 30), "nl": (35, 45, 26, 30),
    "schengen": (35, 45, 32, 36), "za": (35, 45, 29, 34), "kr": (35, 45, 32, 36), "uk": (35, 45, 29, 34),
    "sg": (35, 45, 32, 36),
}


@pytest.mark.parametrize("pid", sorted(OFFICIAL_MM))
def test_official_mm_values(pid: str) -> None:
    w, h, lo, hi = OFFICIAL_MM[pid]
    p = get_preset(pid)
    assert (p.width_mm, p.height_mm) == (w, h)
    assert p.head_min * p.height_mm == pytest.approx(lo)
    assert p.head_max * p.height_mm == pytest.approx(hi)


def test_official_fraction_values() -> None:
    for pid in ("us", "us-dv", "in-oci", "in-visa"):  # 2×2 in: head 1–1⅜ in, eyes 1⅛–1⅜ in from bottom
        p = get_preset(pid)
        assert (p.head_min, p.head_max) == pytest.approx((0.50, 0.6875))
        assert (p.eye_min, p.eye_max) == pytest.approx((0.5625, 0.6875))
    assert get_preset("ca-digital").output_size() == (1200, 1800)
    assert (get_preset("ca-digital").head_min, get_preset("ca-digital").head_max) == (0.45, 0.50)
    nzv = get_preset("nz-visa")
    assert nzv.output_size() == (900, 1200) and nzv.head_min == nzv.head_max == 0.75
    assert (nzv.eye_min, nzv.eye_max) == (0.50, 0.70)
    assert get_preset("sg").output_size() == (400, 514)
    assert get_preset("kr").output_size() == (413, 531)
    assert (get_preset("in").head_min, get_preset("in").head_max) == (0.80, 0.85)


def test_margins_match_official_layout() -> None:
    cn = get_preset("cn")  # crown 3–5 mm below the top edge, chin at least 7 mm above the bottom
    assert (cn.top_min * 48, cn.top_max * 48) == pytest.approx((3, 5))
    assert 48 - (cn.top_max + cn.head_max) * 48 >= 7 - 1e-9
    jp = get_preset("jp")  # crown 4 ± 2 mm below the top edge
    assert (jp.top_min * 45, jp.top_max * 45) == pytest.approx((2, 6))


def test_backgrounds_follow_authority() -> None:
    # White is rejected by these authorities (light grey / cream / light blue required).
    for pid in ("fr", "uk", "za", "nz-visa"):
        assert get_preset(pid).background != "#ffffff", pid
    assert get_preset("nl").dpi == 400  # Fotomatrix 2020: prints at least 400 dpi


def test_unsubstantiated_presets_removed() -> None:
    # No official head-size rule could be verified for these (see report); use Custom instead.
    assert "es" not in PRESETS
    assert "nz" not in PRESETS


def test_list_presets_serialisable() -> None:
    rows = list_presets()
    assert [r["id"] for r in rows] == IDS
    for r in rows:
        assert {"width_px", "height_px", "head_min", "source"} <= r.keys()


def test_papers_untouched() -> None:
    assert {"4x6", "5x7", "10x15", "13x18", "a5", "a4", "letter"} <= PAPERS.keys()


# C3: authorities that reject edited backgrounds / measure the head to the top of the skull.
NO_BACKGROUND_EDIT = {"ca", "ca-digital", "ca-visa", "in", "kr", "sg", "uk"}
SKULL_CROWN = {"ca", "ca-digital", "ca-visa", "fr", "kr", "schengen", "uk"}


def test_background_edit_and_crown_flags() -> None:
    assert {p.id for p in ALL if not p.background_edit_allowed} == NO_BACKGROUND_EDIT
    assert {p.id for p in ALL if not p.crown_includes_hair} == SKULL_CROWN
    for r in list_presets():
        assert isinstance(r["background_edit_allowed"], bool) and isinstance(r["crown_includes_hair"], bool)
    custom = custom_preset(35, 45)
    assert custom.background_edit_allowed is True and custom.crown_includes_hair is True
    assert custom_preset(35, 45, crown_includes_hair=False).crown_includes_hair is False


@pytest.mark.parametrize("p", ALL, ids=IDS)
def test_flags_are_backed_by_the_notes(p: PassportPreset) -> None:
    """Each flag must be traceable to the preset's own notes (which cite its source)."""
    notes = p.notes.lower()
    edit_words = ("unaltered", "altered in any way", "not accepted")
    skull_words = ("skull", "excluding hair", "hair excluded", "not the top of the hair", "hairstyle ignored")
    assert p.background_edit_allowed != any(w in notes for w in edit_words), p.id
    if not p.crown_includes_hair:
        assert any(w in notes for w in skull_words), p.id
    else:
        assert "skull" not in notes and "excluding hair" not in notes, p.id
