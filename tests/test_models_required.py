"""Model integrity: pinned downloads (scripts/fetch_models.py) and the HEYP_REQUIRE_MODELS switch.

pytest.ini also loads this module as a plugin (``-p tests.test_models_required``). With
HEYP_REQUIRE_MODELS=1, which the dev image sets, every test skipped because a model is missing is
reported as a failure, so a broken model download can no longer produce a green suite.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASTRONAUT = Path(__file__).parent / "fixtures" / "astronaut.png"
_MODEL_SKIP = re.compile(r"model|detector|u²-net|u2net|yunet|astronaut", re.IGNORECASE)


def models_required() -> bool:
    """True when HEYP_REQUIRE_MODELS asks for model-dependent tests to fail instead of skipping."""
    return os.environ.get("HEYP_REQUIRE_MODELS", "").strip().lower() in {"1", "true", "yes", "on"}


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """Under HEYP_REQUIRE_MODELS, report model-related skips as failures."""
    report = yield
    if report.skipped and models_required() and not hasattr(report, "wasxfail"):
        reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
        if _MODEL_SKIP.search(reason):
            report.outcome = "failed"
            report.longrepr = f"HEYP_REQUIRE_MODELS=1 but the test was skipped: {reason}"
    return report


# --- scripts/fetch_models.py ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def fm():
    spec = importlib.util.spec_from_file_location("heyp_fetch_models", ROOT / "scripts" / "fetch_models.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def blobs(tmp_path, fm):
    """A pinned 'model' plus good, junk, truncated and oversized copies served from file:// URLs."""
    good = os.urandom(150_000)
    files = {"good": good, "junk": b"<html><body>Rate limited</body></html>".ljust(150_000, b" "),
             "short": good[:100_000], "long": good + b"x"}
    src = tmp_path / "src"
    src.mkdir()
    urls = {}
    for key, data in files.items():
        (src / key).write_bytes(data)
        urls[key] = (src / key).as_uri()
    dest = tmp_path / "models"
    dest.mkdir()
    model = fm.Model(hashlib.sha256(good).hexdigest(), len(good), (urls["good"],))
    return model, urls, dest, good


def test_fetch_rejects_payload_that_does_not_match_the_pin(fm, blobs):
    model, urls, dest, _ = blobs
    for bad in ("junk", "short", "long"):
        with pytest.raises(SystemExit, match="could not fetch m.onnx"):
            fm.fetch("m.onnx", model, str(dest), urls=[urls[bad]])
        assert list(dest.iterdir()) == []  # nothing (not even a .part) is left behind


def test_fetch_falls_back_to_a_mirror_only_when_its_bytes_match(fm, blobs):
    model, urls, dest, good = blobs
    path = fm.fetch("m.onnx", model, str(dest), urls=[urls["junk"], "file:///nonexistent/m.onnx", urls["good"]])
    assert Path(path).read_bytes() == good
    assert sorted(p.name for p in dest.iterdir()) == ["m.onnx"]


def test_fetch_replaces_a_tampered_existing_file(fm, blobs):
    model, urls, dest, good = blobs
    (dest / "m.onnx").write_bytes(b"\0" * model.size)  # right size, wrong digest
    assert fm.fetch("m.onnx", model, str(dest), urls=[urls["good"]]) == str(dest / "m.onnx")
    assert (dest / "m.onnx").read_bytes() == good
    (dest / "m.onnx").write_bytes(b"tampered")
    with pytest.raises(SystemExit):
        fm.fetch("m.onnx", model, str(dest), urls=[urls["junk"]])
    assert not (dest / "m.onnx").exists()


def test_fetch_keeps_a_verified_file_without_downloading(fm, blobs):
    model, _, dest, good = blobs
    (dest / "m.onnx").write_bytes(good)
    assert fm.fetch("m.onnx", model, str(dest), urls=["file:///nonexistent/m.onnx"]) == str(dest / "m.onnx")


def test_fetch_tries_heyp_models_mirror_first(fm, blobs, monkeypatch):
    model, urls, dest, good = blobs
    mirror = Path(urls["good"][len("file://"):]).parent / "mirror"
    mirror.mkdir()
    (mirror / "m.onnx").write_bytes(good)
    monkeypatch.setenv("HEYP_MODELS_MIRROR", f" {mirror.as_uri()}/ ")
    assert fm.mirror_urls("m.onnx") == [f"{mirror.as_uri()}/m.onnx"]
    broken = model._replace(urls=(urls["junk"],))  # only the mirror has the pinned bytes
    assert Path(fm.fetch("m.onnx", broken, str(dest))).read_bytes() == good
    assert fm.mirror_urls("m.onnx", {"HEYP_MODELS_MIRROR": "https://a.example/m, ,https://b.example"}) == [
        "https://a.example/m/m.onnx", "https://b.example/m.onnx"]


def test_main_exits_nonzero_when_no_source_matches(fm, blobs, monkeypatch):
    model, urls, dest, _ = blobs
    monkeypatch.setattr(fm, "MODELS", {"m.onnx": model._replace(urls=(urls["junk"],))})
    monkeypatch.delenv("HEYP_MODELS_MIRROR", raising=False)
    with pytest.raises(SystemExit) as info:
        fm.main([str(dest)])
    assert info.value.code not in (0, None)


def test_pins_cover_the_models_the_app_loads(fm):
    from app.imaging import face, segment

    assert set(fm.MODELS) == {face.YUNET_FILE, segment.MODEL_FILE}
    for model in fm.MODELS.values():
        assert re.fullmatch(r"[0-9a-f]{64}", model.sha256) and model.size > 100_000
        assert model.urls and all(u.startswith("https://") for u in model.urls)
        assert not any(re.search(r"/(raw|resolve)/(main|master)/", u) for u in model.urls)  # immutable refs


# --- the installed models (dev and runtime images) --------------------------------------------------

def _models_dir() -> Path:
    return Path(os.environ.get("HEYP_MODELS_DIR", "/opt/heyp/models"))


@pytest.mark.parametrize("name", ["face_detection_yunet_2023mar.onnx", "u2net_human_seg.onnx"])
def test_installed_model_matches_its_pin(fm, name):
    path = _models_dir() / name
    if not path.is_file():
        pytest.skip(f"model {name} not installed")
    assert fm.verify(str(path), fm.MODELS[name]), f"{path} does not match the pinned size/sha256"


def test_face_detector_loads_and_finds_a_face():
    from PIL import Image

    from app.imaging import face

    if face.backend() != "yunet":
        pytest.skip("YuNet face detector model not installed")
    with Image.open(ASTRONAUT) as img:
        faces = face.detect(img.convert("RGB"))
    assert len(faces) >= 1 and faces[0].score >= face.MIN_SCORE


def test_segmentation_model_loads_and_separates_person_from_background():
    from PIL import Image

    from app.imaging import segment

    if not segment.available():
        pytest.skip("U²-Net model not installed")
    with Image.open(ASTRONAUT) as img:
        mask = segment.raw_mask(img.convert("RGB"))  # loads the ONNX session: catches truncated files too
    assert mask.shape == (segment.INPUT_SIZE, segment.INPUT_SIZE)
    assert 0.05 < float((mask > 0.5).mean()) < 0.95


# --- the HEYP_REQUIRE_MODELS plugin -----------------------------------------------------------------

def _run_pytest(tmp_path: Path, require: str | None) -> subprocess.CompletedProcess:
    (tmp_path / "test_sample.py").write_text(textwrap.dedent("""
        import pytest

        @pytest.mark.skipif(True, reason="no face detector model installed")
        def test_marked():
            pass

        def test_inline():
            pytest.skip("U²-Net model not installed")

        def test_unrelated():
            pytest.skip("AVIF encoder not built")
    """))
    env = {k: v for k, v in os.environ.items() if k != "HEYP_REQUIRE_MODELS"}
    if require is not None:
        env["HEYP_REQUIRE_MODELS"] = require
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-rA", "-c", str(ROOT / "pytest.ini"),
                           "--rootdir", str(ROOT), str(tmp_path / "test_sample.py")],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)


def test_require_models_turns_model_skips_into_failures(tmp_path):
    res = _run_pytest(tmp_path, "1")
    out = res.stdout + res.stderr
    assert res.returncode == 1, out
    assert out.count("HEYP_REQUIRE_MODELS=1 but the test was skipped") >= 2, out
    assert "1 skipped" in out, out  # unrelated skips stay skips


def test_without_require_models_model_skips_stay_skips(tmp_path):
    res = _run_pytest(tmp_path, None)
    assert res.returncode == 0 and "3 skipped" in res.stdout, res.stdout + res.stderr
