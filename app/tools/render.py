"""Web page capture on the renderer service: ``img.html_to_image`` (HTML → JPG/PNG).

Either a web address (``url`` option) or one uploaded HTML file is rendered by heyp-renderer (Chromium) over its
Unix socket; every network request the page makes goes through heyp-egress, which refuses private, loopback,
link-local, metadata and other reserved addresses after resolving the name itself (no DNS-rebinding window).
The checks here only give early, friendly errors and charge the per-client and per-site render limits.
"""
from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.clients import renderer
from app.core.errors import ToolError
from app.core.registry import Tool, register, register_engine_check

HTML_MAX_BYTES = 5 * 1024 * 1024
URL_MAX = 2048
TIMEOUT_S = 120
WIDTHS = {1920: "Desktop HD (1920 px)", 1440: "Desktop (1440 px)", 1200: "Laptop (1200 px)",
          768: "Tablet (768 px)", 390: "Mobile (390 px)"}
Width = Literal[1920, 1440, 1200, 768, 390]


def normalize_url(raw: str) -> tuple[str, str]:
    """``(url, host)`` for a user-typed address; raises ToolError(422) for anything that isn't a web address.
    (Friendly early check only: the renderer re-validates, and the egress proxy enforces the policy.)"""
    raw = (raw or "").strip()
    if not raw:
        raise ToolError(422, "Enter the address of the web page (for example https://example.com)")
    if len(raw) > URL_MAX or any(c in raw for c in "\r\n\t\x00 "):
        raise ToolError(422, "That web address is not valid")
    if "://" not in raw and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:(?!\d)", raw):
        raw = "https://" + raw
    try:
        u = urllib.parse.urlsplit(raw)
        host = (u.hostname or "").rstrip(".").lower()
        _ = u.port
    except ValueError:
        raise ToolError(422, "That web address is not valid") from None
    if u.scheme not in ("http", "https"):
        raise ToolError(422, "Only http:// and https:// web pages can be captured")
    if u.username is not None or u.password is not None:
        raise ToolError(422, "Web addresses with a user name or password can't be captured")
    if not host:
        raise ToolError(422, "That web address has no site name")
    return urllib.parse.urlunsplit(u), host


def _decode_html(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


# ------------------------------------------------------------------------------------------------- options
class HtmlToImageOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field("", max_length=URL_MAX, title="Web page address",
                     description="Leave empty to capture an uploaded HTML file instead",
                     json_schema_extra={"ui": {"widget": "url", "placeholder": "https://example.com"}})
    width: Width = Field(1440, title="Screen width",  # type: ignore[valid-type]
                         json_schema_extra={"ui": {"widget": "select", "labels": {str(k): v for k, v in WIDTHS.items()},
                                                   "unit": "px"}})
    block_cookie_banners: bool = Field(True, title="Hide cookie banners and pop-ups")
    javascript: bool = Field(True, title="Run the page's scripts")
    delay: float = Field(0, ge=0, le=10, title="Wait before capturing",
                         json_schema_extra={"ui": {"widget": "slider", "unit": "s", "step": 0.5}})
    full_page: bool = Field(True, title="Whole page (not just the first screen)")
    format: Literal["jpg", "png"] = Field("jpg", title="Format",
                                          json_schema_extra={"ui": {"widget": "segmented",
                                                                    "labels": {"jpg": "JPG", "png": "PNG"}}})
    quality: int = Field(85, ge=30, le=100, title="JPG quality",
                         json_schema_extra={"ui": {"widget": "slider", "show_if": {"format": "jpg"}}})

    @field_validator("url")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


# ---------------------------------------------------------------------------------------------- run
def _validate(stored: list, opts: HtmlToImageOptions) -> None:
    """Submit-time check (registry ``validate``): exactly one of a web address or an HTML file."""
    if stored and opts.url:
        raise ToolError(422, "Use either a web address or an HTML file, not both")
    if not stored:
        normalize_url(opts.url)  # 422 for an empty or malformed address, before the job is queued


def _source(ctx, opts: HtmlToImageOptions) -> tuple[dict[str, Any], str, str]:
    """``(payload source, stem for the output name, host or "")`` from the url option or the HTML input."""
    if ctx.inputs and opts.url:
        raise ToolError(422, "Use either a web address or an HTML file, not both")
    if ctx.inputs:
        inp = ctx.inputs[0]
        path = Path(inp.path)
        if path.stat().st_size > HTML_MAX_BYTES:
            raise ToolError(413, f"HTML files are limited to {HTML_MAX_BYTES >> 20} MB")
        html = _decode_html(path.read_bytes())
        ctx.limit("renders")
        stem = Path(inp.name).stem or "page"
        return {"html": html}, stem, ""
    url, host = normalize_url(opts.url)
    ctx.check_render_host(host)  # per-client renders/minute + per (client, destination host)
    return {"url": url}, re.sub(r"[^A-Za-z0-9.-]+", "_", host)[:100] or "page", host


def _render(ctx, payload: dict[str, Any], ext: str) -> renderer.RenderResult:
    def tick(elapsed: float) -> None:
        ctx.progress(min(0.9, 0.1 + elapsed / 30), "Loading the page")

    ctx.progress(0.05, "Opening the page")
    return renderer.render(payload, dest=Path(ctx.workdir) / f"capture.{ext}", deadline=ctx.deadline,
                           check_cancel=ctx.check_cancel, on_tick=tick)


def _payload(opts: HtmlToImageOptions) -> dict[str, Any]:
    timeout_ms = 30_000 + int(opts.delay * 1000)
    return {"viewport_width": int(opts.width), "viewport_height": 900 if opts.width >= 1024 else 844,
            "device_scale_factor": 1 if opts.width >= 768 else 2, "javascript": opts.javascript,
            "hide_cookie_banners": opts.block_cookie_banners, "delay_ms": int(opts.delay * 1000),
            "timeout_ms": min(40_000, timeout_ms)}


def _record(ctx, res: renderer.RenderResult, host: str) -> None:
    info = res.info
    ctx.data.update(host=host or None, final_url=info.get("final_url") if host else None,
                    page_height=info.get("page_height"), truncated=bool(info.get("truncated")),
                    status=info.get("main_status"))
    status = info.get("main_status")
    if isinstance(status, int) and status >= 400:
        ctx.warn(f"The website answered with HTTP {status}; the capture shows the page it returned")
    if info.get("egress_blocked"):
        ctx.warn("Some parts of the page pointed at private or local addresses and were not loaded")


def run_html_to_image(ctx, opts: HtmlToImageOptions) -> None:
    source, stem, host = _source(ctx, opts)
    output = "jpeg" if opts.format == "jpg" else "png"
    payload = {**source, **_payload(opts), "output": output, "full_page": opts.full_page,
               "jpeg_quality": opts.quality}
    res = _render(ctx, payload, opts.format)
    if res.content_type != f"image/{output}":
        raise ToolError(502, "The page renderer returned an unexpected result")
    _record(ctx, res, host)
    if res.info.get("truncated"):
        ctx.warn("The page is very long; the image shows its top part")
    ctx.progress(0.95, "Saving")
    ctx.add_output(res.path, f"{stem}_html_to_image.{opts.format}", res.content_type)
    ctx.progress(1.0, "Done")


# ---------------------------------------------------------------------------------------------- registry
register_engine_check("renderer", renderer.health)


HTML_TO_IMAGE = register(Tool(  # URL captures take no input file: min_files=0
    id="img.html_to_image", title="HTML to image",
    summary="Capture a web page — or an HTML file — as a JPG or PNG, full length or just the first screen.",
    category="Convert", inputs=("html",), options=HtmlToImageOptions, run=run_html_to_image,
    min_files=0, max_files=1, engine="renderer", lane="render", timeout_s=TIMEOUT_S, validate=_validate,
    limits={"max_mb": HTML_MAX_BYTES >> 20},
    outputs="<site or file>_html_to_image.jpg|png",
))

__all__ = ["HTML_TO_IMAGE", "normalize_url"]
