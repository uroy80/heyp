"""img.html_to_image (app/tools/render.py) with the renderer client mocked: registration, URL
checks, what is sent to the renderer, render limits, outputs, warnings and errors — plus repository checks that
keep the service images honest (shared guard.py, pinned Playwright, seccomp profile, compose fragment)."""
from __future__ import annotations

import inspect
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.clients import renderer
from app.core import registry
from app.core.errors import Cancelled, ToolError, error_info
from app.core.jobs import JobContext
from app.tools import render
from app.tools.render import HtmlToImageOptions, normalize_url

ROOT = Path(__file__).resolve().parents[1]
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@dataclass
class FakeInput:
    id: str
    name: str
    path: Path
    kind: str = "html"
    mime: str | None = None
    info: dict[str, Any] = field(default_factory=dict)


class FakeContext:
    """The parts of app.core.jobs.JobContext the capture tool uses (checked against the real class below)."""

    def __init__(self, inputs, workdir: Path, cancel_after: int = 10**9):
        self.inputs = inputs
        self.workdir = workdir
        self.deadline = time.monotonic() + 300
        self.data: dict[str, Any] = {}
        self.warnings: list[str] = []
        self.outputs: list[dict[str, Any]] = []
        self.progress_log: list[tuple[float, str]] = []
        self.limits: list[tuple[str, str]] = []
        self._checks = 0
        self._cancel_after = cancel_after

    def check_cancel(self) -> None:
        self._checks += 1
        if self._checks > self._cancel_after:
            raise Cancelled()

    def progress(self, fraction: float, message: str = "") -> None:
        self.progress_log.append((fraction, message))

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def add_output(self, src, name: str, mime: str | None = None, *, kind: str | None = None) -> dict[str, Any]:
        data = bytes(src) if isinstance(src, (bytes, bytearray)) else Path(src).read_bytes()
        info = {"id": f"out{len(self.outputs)}", "name": name, "mime": mime, "data": data}
        self.outputs.append(info)
        return info

    def limit(self, kind: str, amount: float = 1.0, scope: str = "") -> None:
        self.limits.append((kind, scope))

    def check_render_host(self, host: str) -> None:
        self.limits.append(("render_host", host))


def test_fake_context_matches_the_real_job_context():
    for name in ("check_cancel", "progress", "warn", "add_output", "limit", "check_render_host"):
        assert callable(getattr(JobContext, name, None)), name
    params = inspect.signature(JobContext.add_output).parameters
    assert list(params)[:4] == ["self", "src", "name", "mime"]
    src = inspect.getsource(JobContext.__init__)
    for attr in ("self.inputs", "self.workdir", "self.deadline", "self.data"):
        assert attr in src, attr


class FakeRender:
    def __init__(self, ctype="image/png", body=PNG, info=None, error=None):
        self.calls, self.ctype, self.body, self.info, self.error = [], ctype, body, info or {}, error

    def __call__(self, payload, *, dest, deadline=None, check_cancel=None, on_tick=None, **_):
        self.calls.append({"payload": payload, "deadline": deadline})
        if check_cancel:
            check_cancel()
        if on_tick:
            on_tick(2.0)
        if self.error:
            raise self.error
        Path(dest).write_bytes(self.body)
        return renderer.RenderResult(Path(dest), self.ctype, dict(self.info))


@pytest.fixture
def fake_render(monkeypatch):
    def install(**kw) -> FakeRender:
        f = FakeRender(**kw)
        monkeypatch.setattr(renderer, "render", f)
        return f
    return install


@pytest.fixture
def ctx(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    return FakeContext([], work)


def html_ctx(tmp_path, html: bytes, name="page.html") -> FakeContext:
    p = tmp_path / "in.html"
    p.write_bytes(html)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return FakeContext([FakeInput("f" * 32, name, p, "html", "text/html", {"format": "html"})], work)


# ------------------------------------------------------------------------------------------ registration
def test_registered():
    t = registry.get("img.html_to_image")
    assert (t.category, t.engine, t.lane, t.inputs, t.max_files) == ("Convert", "renderer", "render", ("html",), 1)
    assert t.min_files == 0  # a web address alone is enough
    assert registry.options_schema(t).get("additionalProperties") is False


def test_renderer_engine_check(monkeypatch, tmp_path):
    monkeypatch.setenv("HEYP_RENDERER_SOCKET", str(tmp_path / "none.sock"))
    registry.register_engine_check("renderer", renderer.health)
    ok, reason = registry.engine_status("renderer")
    assert not ok and reason == renderer.NOT_CONFIGURED


@pytest.mark.parametrize("raw,url,host", [
    ("example.com", "https://example.com", "example.com"), ("  https://Example.COM/a?b=1  ", "https://Example.COM/a?b=1",
                                                            "example.com"),
    ("http://example.com:443/", "http://example.com:443/", "example.com"), ("example.com:8080", "https://example.com:8080",
                                                                           "example.com"),
])
def test_normalize_url(raw, url, host):
    assert normalize_url(raw) == (url, host)


@pytest.mark.parametrize("raw", ["", "   ", "file:///etc/passwd", "javascript:alert(1)", "data:text/html,x",
                                 "ftp://example.com", "http://user:pw@example.com", "http://[::1", "http://",
                                 "https://exa mple.com", "http://example.com/\r\nX: y", "https://" + "a" * 2050,
                                 "http://example.com:99999/", "chrome://settings", "view-source:https://example.com"])
def test_normalize_url_rejects(raw):
    with pytest.raises(ToolError) as e:
        normalize_url(raw)
    assert e.value.status == 422


@pytest.mark.parametrize("model,raw", [
    (HtmlToImageOptions, {"width": 1000}), (HtmlToImageOptions, {"format": "gif"}), (HtmlToImageOptions, {"quality": 5}),
    (HtmlToImageOptions, {"delay": 30}), (HtmlToImageOptions, {"url": "x" * 3000}), (HtmlToImageOptions, {"evil": 1}),
    (HtmlToImageOptions, {"page_size": "A4"}), (HtmlToImageOptions, {"full_page": "maybe"}),
])
def test_bad_options(model, raw):
    with pytest.raises(ValidationError):
        model.model_validate(raw)


# --------------------------------------------------------------------------------------------------- runs
def test_url_to_jpg(ctx, fake_render):
    fake = fake_render(ctype="image/jpeg", body=b"\xff\xd8\xff jpg",
                       info={"final_url": "https://www.example.com/", "page_height": 4000, "main_status": 200})
    render.run_html_to_image(ctx, HtmlToImageOptions(url="www.example.com", width=1920, quality=70, delay=1.5))
    p = fake.calls[0]["payload"]
    assert p["url"] == "https://www.example.com" and "html" not in p
    assert (p["output"], p["jpeg_quality"], p["viewport_width"], p["full_page"]) == ("jpeg", 70, 1920, True)
    assert p["delay_ms"] == 1500 and p["hide_cookie_banners"] is True and p["javascript"] is True
    assert p["timeout_ms"] <= 40_000 and fake.calls[0]["deadline"] == ctx.deadline
    assert ctx.limits == [("render_host", "www.example.com")]
    assert ctx.outputs[0]["name"] == "www.example.com_html_to_image.jpg" and ctx.outputs[0]["mime"] == "image/jpeg"
    assert ctx.data["host"] == "www.example.com" and ctx.data["page_height"] == 4000 and not ctx.warnings


def test_png_viewport_only_and_mobile(ctx, fake_render):
    fake = fake_render()
    render.run_html_to_image(ctx, HtmlToImageOptions(url="https://example.com", format="png", full_page=False,
                                                     width=390, block_cookie_banners=False, javascript=False))
    p = fake.calls[0]["payload"]
    assert (p["output"], p["full_page"], p["viewport_width"], p["device_scale_factor"]) == ("png", False, 390, 2)
    assert p["hide_cookie_banners"] is False and p["javascript"] is False
    assert ctx.outputs[0]["name"].endswith(".png")


def test_warnings(ctx, fake_render):
    fake_render(ctype="image/jpeg", info={"truncated": True, "main_status": 404, "egress_blocked": 3})
    render.run_html_to_image(ctx, HtmlToImageOptions(url="example.com"))
    joined = " ".join(ctx.warnings)
    assert "top part" in joined and "HTTP 404" in joined and "private or local" in joined


def test_uploaded_html(tmp_path, fake_render):
    fake = fake_render()
    c = html_ctx(tmp_path, "﻿<h1>Grüße</h1>".encode())
    render.run_html_to_image(c, HtmlToImageOptions(format="png"))
    p = fake.calls[0]["payload"]
    assert p["html"] == "<h1>Grüße</h1>" and "url" not in p
    assert c.limits == [("renders", "")] and c.outputs[0]["name"] == "page_html_to_image.png"
    assert c.data["host"] is None and c.data["final_url"] is None


@pytest.mark.parametrize("raw,text", [(b"<p>caf\xe9</p>", "<p>café</p>"),
                                      ("<p>ü</p>".encode("utf-16"), "<p>ü</p>")])
def test_uploaded_html_encodings(tmp_path, fake_render, raw, text):
    fake = fake_render()
    render.run_html_to_image(html_ctx(tmp_path, raw), HtmlToImageOptions(format="png"))
    assert fake.calls[0]["payload"]["html"] == text


def test_url_and_file_together_or_neither(tmp_path, ctx, fake_render):
    fake = fake_render()
    with pytest.raises(ToolError) as e:
        render.run_html_to_image(html_ctx(tmp_path, b"<p>x</p>"), HtmlToImageOptions(url="example.com"))
    assert e.value.status == 422 and "not both" in e.value.detail
    with pytest.raises(ToolError) as e:
        render.run_html_to_image(ctx, HtmlToImageOptions())
    assert e.value.status == 422 and "address" in e.value.detail
    assert fake.calls == []


def test_neither_or_both_refused_at_submit_time():
    """The registry ``validate`` hook: POST /api/jobs answers 422 before queueing, not a failed job later."""
    t = render.HTML_TO_IMAGE
    assert t.validate is not None
    with pytest.raises(ToolError) as e:
        t.validate([], t.options())
    assert e.value.status == 422 and "address" in e.value.detail
    with pytest.raises(ToolError) as e:
        t.validate([], t.options(url="ftp://example.com/x"))
    assert e.value.status == 422
    with pytest.raises(ToolError) as e:
        t.validate([object()], t.options(url="example.com"))
    assert "not both" in e.value.detail
    t.validate([], t.options(url="example.com"))  # fine
    t.validate([object()], t.options())  # an uploaded HTML file alone is fine


def test_oversized_html(tmp_path, fake_render, monkeypatch):
    fake = fake_render()
    monkeypatch.setattr(render, "HTML_MAX_BYTES", 10)
    with pytest.raises(ToolError) as e:
        render.run_html_to_image(html_ctx(tmp_path, b"<p>" + b"x" * 100 + b"</p>"), HtmlToImageOptions())
    assert e.value.status == 413 and fake.calls == []


def test_wrong_result_type_is_refused(ctx, fake_render):
    fake_render(ctype="image/jpeg")  # a PNG was asked for
    with pytest.raises(ToolError) as e:
        render.run_html_to_image(ctx, HtmlToImageOptions(url="example.com", format="png"))
    assert e.value.status == 502 and ctx.outputs == []


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:fetch('http://169.254.169.254')",
                                 "gopher://127.0.0.1:6379/_FLUSHALL"])
def test_ssrf_schemes_never_reach_the_renderer(ctx, fake_render, url):
    fake = fake_render()
    with pytest.raises(ToolError):
        render.run_html_to_image(ctx, HtmlToImageOptions(url=url))
    assert fake.calls == [] and ctx.limits == []


@pytest.mark.parametrize("url,reason", [("http://169.254.169.254/latest/meta-data/", "link-local"),
                                        ("http://127.0.0.1:8080/api/health", "port-not-allowed"),
                                        ("http://internal.example/", "private")])
def test_ssrf_refusals_from_the_stack_are_clear(ctx, fake_render, url, reason):
    """Private targets are refused by the renderer/egress; the user sees a clear message, the job fails 422."""
    fake_render(error=renderer._error(SimpleNamespace(
        status=422, json={"error": "blocked_target", "detail": reason}, header=lambda *_: "")))
    with pytest.raises(renderer.RenderError) as e:
        render.run_html_to_image(ctx, HtmlToImageOptions(url=url))
    info = error_info(e.value)
    assert info["status"] == 422
    assert "heyp" in info["detail"] or "standard ports" in info["detail"]
    assert ctx.outputs == []


def test_render_limits_are_charged_before_rendering(ctx, fake_render):
    fake = fake_render()

    def limited(host):
        raise ToolError(429, "Too many renders of that website — try again in a minute")
    ctx.check_render_host = limited
    with pytest.raises(ToolError) as e:
        render.run_html_to_image(ctx, HtmlToImageOptions(url="example.com"))
    assert e.value.status == 429 and fake.calls == []


# ------------------------------------------------------------------------------------------ repository
def test_guard_copies_are_identical():
    assert (ROOT / "services/egress/guard.py").read_bytes() == (ROOT / "services/renderer/guard.py").read_bytes()


def test_playwright_is_pinned_and_hash_locked():
    req = (ROOT / "services/renderer/requirements.txt").read_text()
    lock = (ROOT / "services/renderer/requirements.lock").read_text()
    pin = re.search(r"^playwright==(\d+\.\d+\.\d+)$", req, re.M).group(1)
    assert pin == "1.63.0"  # released 2026-09-15 (>= 14 days); 1.64.0 becomes eligible on 2026-10-27
    assert f"playwright=={pin} \\" in lock and lock.count("--hash=sha256:") > 20


def test_service_images_pin_the_base_digest_and_run_non_root():
    for svc in ("renderer", "egress"):
        text = (ROOT / f"services/{svc}/Dockerfile").read_text()
        assert re.search(r"^ARG PYTHON_IMAGE=python:3\.12\.\d+-slim-trixie@sha256:[0-9a-f]{64}$", text, re.M), svc
        runtime = text.split("AS runtime", 1)[1]
        assert re.search(r"^USER (renderer|egress)$", runtime, re.M) and "HEALTHCHECK" in runtime, svc
        assert text.rsplit("\nFROM ", 1)[1].startswith("base AS runtime"), svc  # runtime = last (default) stage


def test_seccomp_profile_allows_only_the_userns_additions():
    prof = json.loads((ROOT / "services/seccomp/chromium-userns.json").read_text())
    assert prof["defaultAction"] == "SCMP_ACT_ERRNO"
    unconditional = {n for rule in prof["syscalls"] if rule["action"] == "SCMP_ACT_ALLOW" and not rule.get("args")
                     and not rule.get("includes") and not rule.get("excludes") for n in rule["names"]}
    assert {"clone", "unshare", "chroot"} <= unconditional
    assert not {"mount", "ptrace", "kexec_load", "init_module", "bpf"} & unconditional


def test_compose_fragment_isolates_the_services():
    text = (ROOT / "services/compose.services.yml").read_text()
    for needle in ("render-sbx:", "egress-wan:", "internal: true",
                   "com.docker.network.bridge.gateway_mode_ipv4: isolated", "seccomp=", "HEYP_RENDER_CHROMIUM_SANDBOX",
                   "HEYP_EGRESS_DENY_CIDRS", "HEYP_RENDERER_SOCKET", "read_only: true",
                   "cap_drop: [ALL]", "no-new-privileges:true", "/healthz"):
        assert needle in text, needle
    renderer_block = text.split("\n  renderer:", 1)[1].split("\n  egress:", 1)[0]
    assert "networks: [render-sbx]" in renderer_block
    assert "docs" not in text  # heyp has no document service


def test_tool_deadline_is_respected_by_the_clients(ctx, fake_render):
    fake = fake_render(ctype="image/jpeg")
    ctx.deadline = time.monotonic() + 42
    render.run_html_to_image(ctx, HtmlToImageOptions(url="example.com"))
    assert fake.calls[0]["deadline"] == ctx.deadline
