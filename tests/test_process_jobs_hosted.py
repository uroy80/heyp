"""Long exports behind a proxy (review img-01 / UIH-01 / OPS-5 / UIH-10): in hosted mode /api/process refuses
work that could outlive Cloudflare's 100 s (Apache 60 s) and points at the img.process job; animated inputs saved
as a still say so."""
from __future__ import annotations

import io
import json
import time
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import main as main_module
from app.config import Settings
from app.main import create_app

HOSTED = dict(mode="hosted", operator_name="Ada Operator", operator_email="ops@example.org",
              public_url="https://heyp.example.org")


def png(size=(64, 48), color=(200, 40, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def gif(frames: int = 3, size=(40, 30)) -> bytes:
    images = [Image.new("RGB", size, (i * 80 % 256, 50, 200 - i * 40 % 200)) for i in range(frames)]
    buf = io.BytesIO()
    images[0].save(buf, "GIF", save_all=True, append_images=images[1:], duration=120, loop=0)
    return buf.getvalue()


@pytest.fixture
def make_client(tmp_path):
    clients = []

    def make(**kw) -> TestClient:
        settings = Settings(**{"tmp_dir": str(tmp_path / f"heyp{len(clients)}"), "max_upload_mb": 30,
                               "max_files": 10, **kw})
        c = TestClient(create_app(settings))
        c.__enter__()
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)
    main_module.set_access_log(False)


def upload(c: TestClient, name: str, data: bytes) -> dict:
    res = c.post("/api/files", files=[("files", (name, data, "application/octet-stream"))])
    assert res.status_code == 200 and not res.json()["errors"], res.text
    return res.json()["files"][0]


def test_hosted_refuses_heavy_sync_exports_and_points_at_the_job(make_client):
    c = make_client(**HOSTED)
    f = upload(c, "a.png", png())
    res = c.post("/api/process", json={"ids": [f["id"]], "ops": [{"op": "upscale", "factor": 2}]})
    assert res.status_code == 422, res.text
    assert res.headers["x-heyp-use-job"] == "img.process"
    assert "img.process" in res.json()["detail"]
    bg = c.post("/api/process", json={"ids": [f["id"]], "ops": [{"op": "background", "mode": "remove"}],
                                      "output": {"format": "png"}})
    assert bg.status_code == 422 and bg.headers["x-heyp-use-job"] == "img.process"
    # light work still downloads directly
    ok = c.post("/api/process", json={"ids": [f["id"]], "ops": [{"op": "rotate", "angle": 90}]})
    assert ok.status_code == 200 and ok.headers["content-type"] == "image/jpeg"


def test_local_mode_keeps_the_direct_download(make_client):
    c = make_client()
    f = upload(c, "a.png", png())
    res = c.post("/api/process", json={"ids": [f["id"]], "ops": [{"op": "rotate", "angle": 90}]})
    assert res.status_code == 200


def test_hosted_sync_exports_count_against_the_job_bucket(make_client):
    c = make_client(**HOSTED, limit_jobs_minute=1)
    f = upload(c, "a.png", png())
    assert c.post("/api/process", json={"ids": [f["id"]]}).status_code == 200
    res = c.post("/api/process", json={"ids": [f["id"]]})
    assert res.status_code == 429 and int(res.headers["retry-after"]) >= 1


def test_the_job_runs_the_export_over_http(make_client):
    c = make_client(**HOSTED)
    assert "img.process" in [t["id"] for t in c.get("/api/tools", params={"app": "heyp"}).json()["tools"]]
    a, b = upload(c, "a.png", png()), upload(c, "b.png", png(color=(0, 90, 200)))
    res = c.post("/api/jobs", json={"tool": "img.process", "file_ids": [a["id"], b["id"]],
                                    "options": {"ops": [{"op": "rotate", "angle": 90}], "output": {"format": "webp"}}})
    assert res.status_code == 202, res.text
    job_id = res.json()["job_id"]
    body = {}
    for _ in range(300):
        body = c.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("done", "failed", "cancelled"):
            break
        time.sleep(0.05)
    assert body["status"] == "done", body
    names = sorted(f["name"] for f in body["result"]["files"])
    assert names == ["a.webp", "b.webp"]
    archive = body["result"]["zip"]
    assert c.get(f"/api/files/{archive['id']}/download").status_code == 200


def test_animated_input_saved_as_a_still_warns(make_client):
    c = make_client()
    f = upload(c, "bot.gif", gif(4))
    res = c.post("/api/process", json={"ids": [f["id"]], "output": {"format": "jpeg"}})
    assert res.status_code == 200 and res.headers["content-type"] == "image/jpeg"
    warnings = json.loads(unquote(res.headers["x-heyp-warnings"]))
    assert any("only the first frame" in w for w in warnings), warnings
    kept = c.post("/api/process", json={"ids": [f["id"]], "output": {"format": "gif"}})
    assert not json.loads(unquote(kept.headers["x-heyp-warnings"]))
