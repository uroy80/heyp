"""img.process (app/tools/img_process.py): heyp's export pipeline as a job, so long exports don't have to
finish inside one HTTP request (Cloudflare 100 s / Apache 60 s proxy timeouts)."""
from __future__ import annotations

import io as _io
import time
import zipfile

import pytest
from PIL import Image

from app.config import Settings
from app.core import registry
from app.core.errors import ToolError
from app.core.jobs import make_job_manager
from app.imaging import io
from app.store import FileStore
from app.tools import img_process
from tests.conftest import make_gif_bytes, make_jpeg_bytes


@pytest.fixture
def runner(tmp_path):
    settings = Settings.from_env({"HEYP_TMP_DIR": str(tmp_path / "heyp")})
    store = FileStore(tmp_path / "heyp" / "files", ttl=3600)
    store.init()
    manager = make_job_manager(settings, store)
    manager.start()
    yield manager, store
    manager.stop()


def upload(store: FileStore, data: bytes, name: str, **extra) -> str:
    stored = store.save(name, _io.BytesIO(data), max_bytes=50 * 1024 * 1024)
    with Image.open(_io.BytesIO(data)) as im:
        fmt = io.canonical_format(im.format)
        size = im.size
        frames = getattr(im, "n_frames", 1)
    store.set_info(stored.id, kind="image", format=fmt, mime=io.mime_for(fmt), width=size[0], height=size[1],
                   **({"frames": frames} if frames > 1 else {}), **extra)
    return stored.id


def wait(job, timeout: float = 60.0) -> dict:
    end = time.monotonic() + timeout
    while not job.finished_flag and time.monotonic() < end:
        time.sleep(0.02)
    assert job.finished_flag, job.to_json()
    return job.to_json()


def data_of(store: FileStore, info: dict) -> bytes:
    return store.get(info["id"]).path.read_bytes()


def test_registered_as_a_heavy_heyp_job():
    tool = registry.get("img.process")
    assert tool.lane == "heavy" and tool.timeout_s >= 600 and tool.inputs == ("image",)
    schema = registry.options_schema(tool)
    assert {"ops", "ops_by_id", "output"} <= set(schema["properties"])
    assert registry.tool_json(tool)["available"] is True


def test_one_photo_gives_the_file_a_direct_download_would(runner):
    manager, store = runner
    fid = upload(store, make_jpeg_bytes((300, 200)), "holiday.jpg")
    job = manager.submit("img.process", [fid], {
        "ops": [{"op": "resize", "mode": "fit", "width": 150, "height": 150}],
        "output": {"format": "png"}})
    body = wait(job)
    assert body["status"] == "done", body
    (out,) = body["result"]["files"]
    assert body["result"]["zip"] is None
    assert out["name"] == "holiday.png"
    with Image.open(_io.BytesIO(data_of(store, out))) as im:
        assert im.format == "PNG" and im.size == (150, 100)
    assert any(name == "progress" for name, _ in job.events)


def test_a_batch_uses_ops_by_id_and_gets_a_zip(runner):
    manager, store = runner
    a = upload(store, make_jpeg_bytes((200, 100)), "a.jpg")
    b = upload(store, make_jpeg_bytes((200, 100)), "b.jpg")
    job = manager.submit("img.process", [a, b], {
        "ops": [{"op": "rotate", "angle": 90}],
        "ops_by_id": {b: []},
        "output": {"format": "jpeg", "filename": "ignored-for-batches"}})
    body = wait(job)
    assert body["status"] == "done", body
    files = {f["name"]: f for f in body["result"]["files"]}
    assert set(files) == {"a.jpg", "b.jpg"}
    with Image.open(_io.BytesIO(data_of(store, files["a.jpg"]))) as im:
        assert im.size == (100, 200)
    with Image.open(_io.BytesIO(data_of(store, files["b.jpg"]))) as im:
        assert im.size == (200, 100)
    archive = body["result"]["zip"]
    assert archive is not None
    with zipfile.ZipFile(_io.BytesIO(data_of(store, archive))) as zf:
        assert sorted(zf.namelist()) == ["a.jpg", "b.jpg"]
    messages = [data.get("message", "") for name, data in job.events if name == "progress"]
    assert any("(2 of 2)" in m for m in messages), messages


def test_combined_pdf_is_one_file(runner):
    manager, store = runner
    ids = [upload(store, make_jpeg_bytes((120, 80)), f"p{i}.jpg") for i in range(3)]
    body = wait(manager.submit("img.process", ids, {"output": {"format": "pdf", "combine_pdf": True,
                                                               "filename": "album"}}))
    assert body["status"] == "done", body
    (out,) = body["result"]["files"]
    assert out["name"] == "album.pdf"
    assert data_of(store, out).startswith(b"%PDF-")


def test_animation_to_a_still_format_says_so(runner):
    manager, store = runner
    gif = upload(store, make_gif_bytes(4, (60, 40)), "bot.gif")
    body = wait(manager.submit("img.process", [gif], {"output": {"format": "jpeg"}}))
    assert body["status"] == "done", body
    assert any("only the first frame" in w for w in body["warnings"]), body["warnings"]
    kept = wait(manager.submit("img.process", [gif], {"output": {"format": "original"}}))
    assert not any("only the first frame" in w for w in kept["warnings"]), kept["warnings"]


def test_impossible_requests_are_refused_before_queueing(runner):
    manager, store = runner
    fid = upload(store, make_jpeg_bytes((64, 48)), "x.jpg")
    other = upload(store, make_jpeg_bytes((64, 48)), "y.jpg")
    with pytest.raises(ToolError) as bad_key:
        manager.submit("img.process", [fid], {"ops_by_id": {other: []}})
    assert bad_key.value.status == 422
    with pytest.raises(ToolError) as bad_option:
        manager.submit("img.process", [fid], {"ops": [], "ids": [fid]})
    assert bad_option.value.status == 422
    # declared 6000×6000 → a 4× upscale is far beyond one job's work budget (nothing is decoded to refuse it)
    big = upload(store, make_jpeg_bytes((64, 48)), "big.jpg")
    store.set_info(big, width=6000, height=6000)
    with pytest.raises(ToolError) as heavy:
        manager.submit("img.process", [big], {"ops": [{"op": "upscale", "factor": 4}]})
    assert heavy.value.status == 413
    # frame-bomb guard from the recorded frame count
    bomb = upload(store, make_gif_bytes(3, (60, 40)), "bomb.gif")
    store.set_info(bomb, frames=100_000)
    with pytest.raises(ToolError) as frames:
        manager.submit("img.process", [bomb], {"output": {"format": "gif"}})
    assert frames.value.status == 413


def test_too_many_files_for_this_server(tmp_path):
    settings = Settings.from_env({"HEYP_TMP_DIR": str(tmp_path / "heyp"), "HEYP_MAX_FILES": "1"})
    store = FileStore(tmp_path / "heyp" / "files", ttl=3600)
    store.init()
    manager = make_job_manager(settings, store)
    manager.start()
    try:
        ids = [upload(store, make_jpeg_bytes((40, 30)), f"{i}.jpg") for i in range(2)]
        body = wait(manager.submit("img.process", ids, {}))
        assert body["status"] == "failed" and body["error"]["status"] == 422, body
    finally:
        manager.stop()


def test_options_mirror_process_request():
    opts = img_process.ProcessOptions.model_validate({"ops": [{"op": "rotate", "angle": 90}],
                                                      "output": {"format": "jpg"}})
    assert opts.output.format == "jpeg" and opts.ops_for("x")[0].op == "rotate"
