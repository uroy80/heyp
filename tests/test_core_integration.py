"""Integration of every tool module with the app: the registry as /api/tools shows it, and one job per area
through the real HTTP surface (upload → POST /api/jobs → poll / SSE → download → "Continue with" a result).
The renderer service is not running here: its tool must say so."""
from __future__ import annotations

import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import main as main_module, tools
from app.config import Settings
from app.core import registry
from app.main import create_app

# every tool heyp ships, by module
EXPECTED = {
    "img_ai": {"img.upscale", "img.remove_background", "img.blur"},
    "img_process": {"img.process"},
    "img_tools": {"img.make_gif", "img.compress"},
    "render": {"img.html_to_image"},
}


def png(size=(64, 48), color=(200, 40, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def client(tmp_path):
    settings = Settings(tmp_dir=str(tmp_path / "heyp"), max_upload_mb=30, max_files=20)
    with TestClient(create_app(settings)) as c:
        yield c
    main_module.set_access_log(False)


def upload(client: TestClient, *files: tuple[str, bytes, str]) -> list[dict]:
    r = client.post("/api/files", files=[("files", f) for f in files])
    assert r.status_code == 200, r.text
    return r.json()["files"]


def run_job(client: TestClient, tool: str, file_ids: list[str], options: dict | None = None,
            timeout: float = 120.0) -> dict:
    r = client.post("/api/jobs", json={"tool": tool, "file_ids": file_ids, "options": options or {}})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        state = client.get(f"/api/jobs/{job_id}").json()
        if state["status"] in ("done", "failed", "cancelled"):
            return state
        time.sleep(0.05)
    raise AssertionError(f"{tool} did not finish: {state}")


def done(state: dict) -> dict:
    assert state["status"] == "done", state
    return state["result"]


# ------------------------------------------------------------------ registry ↔ tool modules
def test_every_tool_module_loads_and_registers_its_tools():
    assert tools.load() == {}, "a tool module failed to import"
    ids = {t.id for t in registry.all_tools()}
    assert ids == set().union(*EXPECTED.values())
    for module, expected in EXPECTED.items():
        assert module in {m.rsplit(".", 1)[-1] for m in tools.modules()}, module
        assert expected <= ids, f"{module}: missing {sorted(expected - ids)}"


def test_api_tools_lists_every_registered_tool_with_availability(client):
    body = client.get("/api/tools").json()["tools"]
    listed = {t["id"]: t for t in body}
    assert set(listed) == {t.id for t in registry.all_tools()}
    for tool_id, t in listed.items():
        assert isinstance(t["available"], bool) and isinstance(t["reason"], str)
        assert t["options_schema"]["type"] == "object"
        if t["engine"] == "app" and t["id"] in ("img.process", "img.make_gif", "img.compress"):
            assert t["available"] is True, (tool_id, t["reason"])
        if t["engine"] == "renderer":
            assert t["available"] is False and t["reason"], tool_id


def test_url_captures_take_no_input_file(client):
    t = client.get("/api/tools/img.html_to_image").json()
    assert t["min_files"] == 0 and t["max_files"] == 1 and t["inputs"] == ["html"]


def test_config_features_follow_the_engines_and_models(client):
    from app.tools import img_ai

    cfg = client.get("/api/config").json()
    assert cfg["features"]["renderer"] is False
    for key, on in img_ai.features().items():
        if key in ("upscale", "matting", "plates"):
            assert cfg["features"][key] is bool(on)
    assert cfg["limits"]["upscale_max_input_pixels"] > 0
    assert "revision" in cfg
    health = client.get("/api/health").json()
    assert {"renderer", "upscale", "matting", "plates"} <= set(health["features"])


def test_the_capture_tool_answers_503_with_the_reason(client):
    r = client.post("/api/jobs", json={"tool": "img.html_to_image", "file_ids": [],
                                       "options": {"url": "https://example.org"}})
    assert r.status_code == 503
    assert r.json()["detail"] == client.get("/api/tools/img.html_to_image").json()["reason"] != ""


# ------------------------------------------------------------------ one job per area, end to end
def test_export_job_writes_one_file_per_photo_and_a_zip(client):
    a, b = upload(client, ("one.png", png(), "image/png"), ("two.png", png(color=(0, 90, 200)), "image/png"))
    result = done(run_job(client, "img.process", [a["id"], b["id"]],
                          {"ops": [{"op": "resize", "mode": "scale", "scale": 50}], "output": {"format": "webp"}}))
    assert [f["name"] for f in result["files"]] == ["one.webp", "two.webp"]
    assert all(f["kind"] == "image" and (f["width"], f["height"]) == (32, 24) for f in result["files"])
    r = client.get(f"/api/files/{result['zip']['id']}/download")
    assert sorted(zipfile.ZipFile(io.BytesIO(r.content)).namelist()) == ["one.webp", "two.webp"]


def test_export_job_combines_photos_into_one_pdf(client):
    a, b = upload(client, ("one.png", png(), "image/png"), ("two.png", png(color=(0, 90, 200)), "image/png"))
    result = done(run_job(client, "img.process", [a["id"], b["id"]],
                          {"output": {"format": "pdf", "combine_pdf": True, "filename": "both"}}))
    (out,) = result["files"]
    assert out["kind"] == "pdf" and out["mime"] == "application/pdf" and out["name"] == "both.pdf"
    r = client.get(f"/api/files/{out['id']}/download")
    assert r.status_code == 200 and r.content.startswith(b"%PDF-") and r.content.count(b"/Type /Page\n") == 2
    assert client.get(f"/api/files/{out['id']}/preview").status_code == 415  # a PDF is not an image to edit


def test_make_gif_result_describes_the_animation_and_opens_in_heyp(client):
    frames = upload(client, *[(f"f{i}.png", png(color=(60 * i, 100, 200 - 50 * i)), "image/png") for i in range(3)])
    result = done(run_job(client, "img.make_gif", [f["id"] for f in frames], {"delay_ms": 200}))
    (out,) = result["files"]
    assert out["kind"] == "image" and out["format"] == "gif" and out["mime"] == "image/gif"
    assert out["frames"] == 3 and (out["width"], out["height"]) == (64, 48)
    info = client.get(f"/api/files/{out['id']}").json()  # "Continue with": the result is an ordinary file
    assert info["kind"] == "image" and info["width"] == 64
    r = client.post("/api/preview", json={"ids": [out["id"]], "ops": [], "output": {"format": "png"}})
    assert r.status_code == 200, r.text


def test_compress_and_blur_jobs_on_images(client):
    (a,) = upload(client, ("photo.png", png((120, 80)), "image/png"))
    (small,) = done(run_job(client, "img.compress", [a["id"]]))["files"]
    assert small["kind"] == "image" and small["format"] == "png" and small["width"] == 120
    state = run_job(client, "img.blur", [a["id"]], {"detect": [], "mode": "pixelate",
                                                     "regions": [{"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}]})
    (blurred,) = done(state)["files"]
    assert blurred["kind"] == "image" and blurred["height"] == 80
    assert blurred["name"].startswith("photo_") and " (2)" not in blurred["name"]


def test_upscale_job_when_its_model_is_installed(client):
    from app.tools import img_ai

    if not img_ai.features().get("upscale"):
        pytest.skip("upscale model not installed")
    (a,) = upload(client, ("tiny.png", png((32, 24)), "image/png"))
    (big,) = done(run_job(client, "img.upscale", [a["id"]], {"factor": 2}))["files"]
    assert (big["width"], big["height"]) == (64, 48) and big["frames"] == 1


def test_job_events_stream_progress_and_done(client):
    (a,) = upload(client, ("photo.png", png(), "image/png"))
    r = client.post("/api/jobs", json={"tool": "img.compress", "file_ids": [a["id"]], "options": {}})
    job_id = r.json()["job_id"]
    with client.stream("GET", f"/api/jobs/{job_id}/events") as events:
        body = "".join(events.iter_text())
    assert "event: progress" in body and "event: done" in body
