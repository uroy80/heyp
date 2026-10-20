"""app.core.registry: validation, lookup, availability and the /api/tools JSON."""
from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from app.core import registry
from app.core.errors import ToolError
from app.core.registry import Tool


class Opts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    size: str = Field("medium", json_schema_extra={"ui": {"widget": "text"}})
    mode: Literal["a", "b"] = "a"


class LooseOpts(BaseModel):
    size: str = "medium"


def run(ctx, options) -> None:
    pass


def other_run(ctx, options) -> None:
    pass


def make(**kw) -> Tool:
    base = dict(id="img.zz_reg_test", title="Registry test", summary="One line.", category="Edit",
                inputs=("image",), options=Opts, run=run)
    return Tool(**{**base, **kw})


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    for tool_id in ("img.zz_reg_test", "img.zz_reg_other", "img.zz_reg_render"):
        registry.unregister(tool_id)
    registry.clear_engine_cache()


def test_register_and_get():
    tool = registry.register(make(inputs=(".PNG", "jpg", "TIF", "heif")))
    assert registry.get("img.zz_reg_test") is tool
    assert tool.inputs == ("png", "jpeg", "tiff", "heic")  # canonical spellings
    assert tool in registry.all_tools()
    with pytest.raises(KeyError):
        registry.get("img.nope")
    with pytest.raises(TypeError):
        tool.limits["max_files"] = 3  # frozen


@pytest.mark.parametrize(("change", "message"), [
    ({"id": "resize"}, "id must look like"),
    ({"id": "IMG.Resize"}, "id must look like"),
    ({"id": "pdf.merge"}, "id must look like"),
    ({"options": LooseOpts}, "extra='forbid'"),
    ({"options": dict}, "pydantic BaseModel"),
    ({"inputs": ()}, "inputs"),
    ({"min_files": 3, "max_files": 2}, "min_files"),
    ({"min_files": 0, "max_files": 0}, "min_files"),
    ({"min_files": -1}, "min_files"),
    ({"lane": "turbo"}, "lane"),
    ({"lane": "docs"}, "lane"),
    ({"engine": "gpu"}, "engine"),
    ({"engine": "ai"}, "engine"),
    ({"timeout_s": 0}, "timeout_s"),
    ({"limits": {"max_widgets": 3}}, "unknown limit"),
    ({"limits": {"max_pages": 3}}, "unknown limit"),
    ({"limits": {"max_files": -1}}, "non-negative"),
    ({"limits": {"max_mb": True}}, "non-negative"),
    ({"title": " "}, "required"),
])
def test_invalid_tools_are_refused(change, message):
    with pytest.raises(ValueError, match=message):
        registry.register(make(**change))


def test_tools_may_take_no_input_file():
    """URL captures (renderer) run from options alone: min_files=0, at most one optional file."""
    tool = registry.register(make(min_files=0, max_files=1))
    assert tool.min_files == 0
    assert registry.tool_json(tool)["min_files"] == 0


def test_duplicates():
    registry.register(make())
    registry.register(make())  # the same definition again (module reload) is fine
    with pytest.raises(ValueError, match="already registered"):
        registry.register(make(run=other_run))
    assert registry.register(make(id="img.zz_reg_other", run=other_run, title="Other")).title == "Other"


def test_options_parsing_and_schema():
    tool = registry.register(make())
    assert registry.parse_options(tool, None) == Opts()
    assert registry.parse_options(tool, {"mode": "b"}).mode == "b"
    with pytest.raises(ToolError) as info:
        registry.parse_options(tool, {"mode": "c"})
    assert info.value.status == 422 and info.value.detail.startswith("options.mode:")
    with pytest.raises(ToolError, match="not permitted"):
        registry.parse_options(tool, {"surprise": 1})
    with pytest.raises(ToolError, match="must be an object"):
        registry.parse_options(tool, [1, 2])
    schema = registry.options_schema(tool)
    assert schema["properties"]["size"]["ui"] == {"widget": "text"}


def test_accepts_kinds_and_formats():
    tool = registry.register(make(inputs=("html", "png")))
    assert registry.accepts(tool, "html", "html")
    assert registry.accepts(tool, "image", "png")
    assert not registry.accepts(tool, "image", "jpeg")
    assert not registry.accepts(tool, "other", "png")  # an unknown file named .png is never accepted
    assert registry.describe_inputs(tool) == "HTML files or .png"
    assert registry.describe_inputs(registry.register(make(id="img.zz_reg_other", run=other_run))) == "images"


def test_engine_availability(monkeypatch):
    render_tool = registry.register(make(id="img.zz_reg_render", engine="renderer", lane="render", run=other_run,
                                         title="Render"))
    monkeypatch.setattr(registry, "_engine_checks", {})
    ok, reason = registry.availability(render_tool)
    assert not ok and "page renderer" in reason
    calls = []

    def healthy():
        calls.append(1)
        return True, ""

    registry.register_engine_check("renderer", healthy)
    assert registry.availability(render_tool) == (True, "")
    assert registry.availability(render_tool) == (True, "")
    assert len(calls) == 1  # health answers are cached

    def broken():
        raise ConnectionError("down")

    registry.register_engine_check("renderer", broken)
    ok, reason = registry.availability(render_tool)
    assert not ok and "not responding" in reason
    for engine in ("gpu", "app"):
        with pytest.raises(ValueError):
            registry.register_engine_check(engine, healthy)
    assert registry.engine_status("app") == (True, "")


def test_tool_check_and_json():
    tool = registry.register(make(check=lambda: (False, "Model missing"), limits={"max_files": 50}, max_files=60))
    body = registry.tool_json(tool)
    assert body["available"] is False and body["reason"] == "Model missing"
    assert body["limits"] == {"max_files": 50}
    assert set(body) == {"id", "title", "summary", "category", "inputs", "min_files", "max_files", "options_schema",
                         "limits", "lane", "engine", "timeout_s", "outputs", "available", "reason"}

    def crashing() -> tuple[bool, str]:
        raise RuntimeError("model file unreadable")

    registry.unregister(tool.id)
    broken = registry.register(make(check=crashing))
    assert registry.availability(broken) == (False, "Registry test is not available right now")
