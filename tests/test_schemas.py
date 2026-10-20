"""Request models: op union, bounds, defaults, cross-field validation."""
from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from app.schemas import (OPS_ADAPTER, CropAspectOp, OutputSpec, PassportOp, ProcessRequest, ResizeOp, SheetOp,
                         ratio_value)

ID = "0123456789abcdef0123456789abcdef"


def req(ops=None, output=None, ids=None) -> ProcessRequest:
    body: dict = {"ids": ids or [ID], "ops": ops or []}
    if output is not None:
        body["output"] = output
    return ProcessRequest.model_validate(body)


def bad(ops=None, output=None, ids=None) -> str:
    with pytest.raises(ValidationError) as info:
        req(ops, output, ids)
    return str(info.value)


def test_output_defaults_match_plan():
    out = req().output
    assert out.model_dump() == {
        "format": "jpeg", "quality": 90, "lossless": False, "progressive": True, "chroma": "auto",
        "dpi": None, "strip_metadata": True, "strip_gps": True, "colorspace": "srgb",
        "background": "#ffffff", "max_kb": None, "min_kb": None, "combine_pdf": True, "filename": None,
    }


def test_every_op_parses_with_minimal_fields():
    ops = OPS_ADAPTER.validate_python([
        {"op": "rotate", "angle": 90},
        {"op": "flip", "direction": "horizontal"},
        {"op": "crop", "x": 0, "y": 0, "width": 10, "height": 10},
        {"op": "crop_aspect", "ratio": "16:9"},
        {"op": "trim"},
        {"op": "resize", "mode": "scale", "scale": 50},
        {"op": "pad", "top": 4},
        {"op": "adjust", "brightness": 1.2},
        {"op": "background", "mode": "remove"},
        {"op": "passport", "preset": "us"},
        {"op": "sheet"},
    ])
    assert [o.op for o in ops] == ["rotate", "flip", "crop", "crop_aspect", "trim", "resize", "pad",
                                   "adjust", "background", "passport", "sheet"]


def test_op_defaults():
    rotate, crop, aspect, trim, resize, passport, sheet = OPS_ADAPTER.validate_python([
        {"op": "rotate", "angle": -90}, {"op": "crop", "x": 0, "y": 0, "width": 1, "height": 1},
        {"op": "crop_aspect", "ratio": 1}, {"op": "trim"}, {"op": "resize", "mode": "fit", "width": 100},
        {"op": "passport", "preset": "in"}, {"op": "sheet"},
    ])
    assert (rotate.expand, rotate.fill) == (True, "#ffffff")
    assert crop.unit == "px" and aspect.anchor == "center"
    assert (trim.tolerance, trim.padding) == (12, 0)
    assert (resize.unit, resize.dpi, resize.upscale, resize.resample, resize.background) == \
        ("px", 300, True, "lanczos", "#ffffff")
    assert (passport.background, passport.replace_background, passport.face_index, passport.zoom,
            passport.offset_x, passport.offset_y, passport.level) == (None, True, 0, 1.0, 0.0, 0.0, True)
    assert (sheet.paper, sheet.orientation, sheet.dpi, sheet.gap_mm, sheet.margin_mm, sheet.cut_lines,
            sheet.copies) == ("4x6", "auto", None, 2, 4, True, 0)  # dpi None → pipeline dpi


def test_unknown_op_and_extra_fields_rejected():
    assert "does not match any of the expected tags" in bad([{"op": "sepia"}])
    assert "Extra inputs are not permitted" in bad([{"op": "rotate", "angle": 90, "speed": 2}])
    assert "Extra inputs are not permitted" in bad(output={"format": "png", "colour": "red"})
    with pytest.raises(ValidationError):
        ProcessRequest.model_validate({"ids": [ID], "unknown": 1})


@pytest.mark.parametrize("op", [
    {"op": "rotate", "angle": 361},
    {"op": "rotate", "angle": -360.5},
    {"op": "rotate", "angle": math.nan},
    {"op": "rotate", "angle": math.inf},
    {"op": "rotate", "angle": 10, "fill": "notacolour"},
    {"op": "flip", "direction": "diagonal"},
    {"op": "crop", "x": -1, "y": 0, "width": 10, "height": 10},
    {"op": "crop", "x": 0, "y": 0, "width": 0, "height": 10},
    {"op": "crop", "x": 0.5, "y": 0, "width": 1.2, "height": 1, "unit": "fraction"},
    {"op": "crop_aspect", "ratio": "abc"},
    {"op": "crop_aspect", "ratio": "0:1"},
    {"op": "crop_aspect", "ratio": "1:0"},
    {"op": "crop_aspect", "ratio": -1},
    {"op": "crop_aspect", "ratio": "1:1", "anchor": "top"},
    {"op": "trim", "tolerance": 256},
    {"op": "trim", "padding": -1},
    {"op": "resize", "mode": "fit"},
    {"op": "resize", "mode": "fill", "width": 100},
    {"op": "resize", "mode": "pad", "height": 100},
    {"op": "resize", "mode": "exact", "width": 100},
    {"op": "resize", "mode": "scale"},
    {"op": "resize", "mode": "scale", "scale": 0.5},
    {"op": "resize", "mode": "scale", "scale": 1001},
    {"op": "resize", "mode": "long_edge"},
    {"op": "resize", "mode": "megapixels"},
    {"op": "resize", "mode": "fit", "width": 30001},
    {"op": "resize", "mode": "fit", "width": 0.5},
    {"op": "resize", "mode": "fit", "width": 200, "unit": "in", "dpi": 300},
    {"op": "resize", "mode": "fit", "width": 100, "dpi": 2401},
    {"op": "resize", "mode": "stretch", "width": 100},
    {"op": "pad", "top": 10001},
    {"op": "adjust", "brightness": -0.1},
    {"op": "background", "mode": "blur"},
    {"op": "passport", "preset": "custom"},
    {"op": "passport", "preset": "../us"},
    {"op": "passport", "preset": "us", "zoom": 0.4},
    {"op": "passport", "preset": "us", "zoom": 2.1},
    {"op": "passport", "preset": "us", "offset_x": 1.5},
    {"op": "passport", "preset": "us", "offset_y": -1.01},
    {"op": "passport", "preset": "custom", "custom": {"width_mm": 35, "height_mm": 45, "head_min": 0.8,
                                                      "head_max": 0.7}},
    {"op": "passport", "preset": "custom", "custom": {"width_mm": 35, "height_mm": 45, "eye_min": 0.5}},
    {"op": "sheet", "copies": 201},
    {"op": "sheet", "copies": -1},
    {"op": "sheet", "dpi": 71},
    {"op": "sheet", "dpi": 1201},
    {"op": "sheet", "orientation": "square"},
    {"op": "sheet", "photo_width_mm": 0.001, "photo_height_mm": 0.001},  # API-1: millions of copies
    {"op": "sheet", "photo_width_mm": 4.9},
    {"op": "sheet", "photo_height_mm": 0},
])
def test_invalid_ops(op):
    bad([op])


@pytest.mark.parametrize("output", [
    {"format": "jxl"}, {"quality": 0}, {"quality": 101}, {"dpi": 0}, {"dpi": 2401},
    {"chroma": "411"}, {"colorspace": "p3"}, {"max_kb": 0}, {"max_kb": 100_001}, {"min_kb": -1},
    {"max_kb": 50, "min_kb": 60}, {"background": "nope"}, {"filename": "x" * 201},
])
def test_invalid_output(output):
    bad(output=output)


def test_ids_validation():
    assert "invalid file id" in bad(ids=["../../etc/passwd"])
    assert "invalid file id" in bad(ids=[ID.upper()])
    with pytest.raises(ValidationError):
        ProcessRequest.model_validate({"ids": []})
    # the HEYP_MAX_FILES count is enforced by the endpoints (app settings); the schema only has a structural bound
    assert "at most 10000" in bad(ids=[f"{i:032x}" for i in range(10_001)])


def test_duplicate_ids_are_processed_once():
    """API-3: a repeated id would redo identical work (and pad a ZIP), so it is collapsed in order."""
    assert req(ids=[ID, "f" * 32, ID, ID]).ids == [ID, "f" * 32]


def test_ops_limit():
    req([{"op": "flip", "direction": "vertical"}] * 32)
    bad([{"op": "flip", "direction": "vertical"}] * 33)


def test_valid_bounds_accepted():
    req([
        {"op": "rotate", "angle": 360, "fill": "transparent"},
        {"op": "rotate", "angle": -360, "fill": "#ffffff80"},
        {"op": "crop", "x": 0, "y": 0, "width": 1, "height": 1, "unit": "fraction"},
        {"op": "resize", "mode": "fit", "width": 30000, "height": 1},
        {"op": "resize", "mode": "fit", "width": 2, "unit": "in", "dpi": 2400},
        {"op": "resize", "mode": "scale", "scale": 1000},
        {"op": "passport", "preset": "us", "zoom": 0.5, "offset_x": -1, "offset_y": 1, "background": "lightblue"},
        {"op": "sheet", "copies": 200, "dpi": 1200},
        {"op": "sheet", "copies": 0, "dpi": 72, "photo_width_mm": 5, "photo_height_mm": 5},
    ], output={"quality": 1, "dpi": 2400, "max_kb": 100_000, "min_kb": 0.5})


def test_format_aliases_and_original():
    assert req(output={"format": "JPG"}).output.format == "jpeg"
    assert req(output={"format": "tif"}).output.format == "tiff"
    assert req(output={"format": "heif"}).output.format == "heic"
    assert req(output={"format": "original"}).output.format == "original"


@pytest.mark.parametrize(("raw", "expected"), [
    ("../../x/evil.JPG", "evil"),
    ("report\x00 2026", "report 2026"),
    ("photo.final", "photo.final"),
    ("   ", None),
    ("..", None),
])
def test_filename_is_sanitised(raw, expected):
    assert req(output={"filename": raw}).output.filename == expected


@pytest.mark.parametrize(("ratio", "value"), [("16:9", 16 / 9), ("4/5", 0.8), ("1.5", 1.5), (1.5, 1.5),
                                              ("35x45", 35 / 45), (" 3 : 2 ", 1.5)])
def test_ratio_value(ratio, value):
    assert ratio_value(ratio) == pytest.approx(value)
    assert CropAspectOp(op="crop_aspect", ratio=ratio).ratio == ratio


def test_custom_passport_preset():
    op = PassportOp.model_validate({"op": "passport", "preset": "custom",
                                    "custom": {"width_mm": 35, "height_mm": 45, "dpi": 600,
                                               "head_min": 0.7, "head_max": 0.8, "eye_min": 0.5, "eye_max": 0.6}})
    assert op.custom is not None and op.custom.dpi == 600


def test_resize_unit_conversion_bound():
    ResizeOp.model_validate({"op": "resize", "mode": "long_edge", "size": 100, "unit": "mm", "dpi": 300})
    with pytest.raises(ValidationError, match="px at"):
        ResizeOp.model_validate({"op": "resize", "mode": "long_edge", "size": 3000, "unit": "mm", "dpi": 300})


def test_sheet_and_output_models_standalone():
    assert SheetOp(op="sheet", paper="a4").paper == "a4"
    assert OutputSpec().format == "jpeg"


# ------------------------------------------------------------------ v1.1: ops_by_id (C1), level (C4), sheet dpi (C5)
ID2 = "fedcba9876543210fedcba9876543210"


def test_ops_by_id_replaces_shared_ops_per_file():
    r = ProcessRequest.model_validate({
        "ids": [ID, ID2], "ops": [{"op": "flip", "direction": "vertical"}],
        "ops_by_id": {ID2: [{"op": "crop", "x": 0, "y": 0, "width": 0.5, "height": 1, "unit": "fraction"}]},
    })
    assert [o.op for o in r.ops_for(ID)] == ["flip"]
    assert [o.op for o in r.ops_for(ID2)] == ["crop"]
    assert ProcessRequest.model_validate({"ids": [ID], "ops_by_id": {ID: []}}).ops_for(ID) == []
    assert req().ops_by_id is None and req().ops_for(ID) == []


@pytest.mark.parametrize("ops_by_id", [
    {ID2: []},                                                   # not in ids
    {"../../etc/passwd": []},                                    # not an id at all
    {ID: [{"op": "teleport"}]},                                  # invalid op
    {ID: [{"op": "flip", "direction": "vertical"}] * 33},        # same per-list limit as ops
    {ID: {"op": "flip"}},                                        # not a list
])
def test_ops_by_id_invalid(ops_by_id):
    with pytest.raises(ValidationError):
        ProcessRequest.model_validate({"ids": [ID], "ops_by_id": ops_by_id})


def test_ops_by_id_unknown_key_message():
    with pytest.raises(ValidationError, match="not in ids"):
        ProcessRequest.model_validate({"ids": [ID], "ops_by_id": {ID2: []}})


def test_passport_level_and_sheet_dpi_fields():
    assert PassportOp.model_validate({"op": "passport", "preset": "us", "level": False}).level is False
    assert SheetOp.model_validate({"op": "sheet", "dpi": None}).dpi is None
    assert SheetOp.model_validate({"op": "sheet", "dpi": 400}).dpi == 400
