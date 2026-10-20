"""OPS-7: the models-v1 release bootstrap — ``fetch_models.py --probe/--list`` and ``scripts/publish_models.sh``.

Image builds download heyp's own model exports only from the models-v1 release, so the release has to exist
before CI and Release first run; CI probes the sources first and fails with one clear error otherwise.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "publish_models.sh"


@pytest.fixture(scope="module")
def fm():
    spec = importlib.util.spec_from_file_location("heyp_fetch_models_publish", ROOT / "scripts" / "fetch_models.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def served(tmp_path, fm):
    """A pinned 'model' whose good copy sits in a mirror directory, and a missing 'release' directory."""
    good = os.urandom(120_000)
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    (mirror / "m.onnx").write_bytes(good)
    (mirror / "short.onnx").write_bytes(good[:1000])
    release = (tmp_path / "release").as_uri()  # does not exist: the release was never created
    model = fm.Model(hashlib.sha256(good).hexdigest(), len(good), (f"{release}/m.onnx",))
    return model, mirror, release


# --- fetch_models.py --probe / --list -------------------------------------------------------------------------

def test_probe_reports_a_model_with_no_source(fm, served, capsys):
    model, mirror, _ = served
    assert fm.probe_all({"m.onnx": model}, env={}) == ["m.onnx"]
    assert "m.onnx: NO SOURCE" in capsys.readouterr().out
    assert fm.probe_all({"m.onnx": model}, env={"HEYP_MODELS_MIRROR": mirror.as_uri()}) == []
    assert f"m.onnx: ok <- {mirror.as_uri()}/m.onnx" in capsys.readouterr().out


def test_probe_rejects_a_source_with_the_wrong_size(fm, served):
    model, mirror, _ = served
    with pytest.raises(fm.IntegrityError, match="expected 120000"):
        fm.probe((mirror / "short.onnx").as_uri(), model)
    fm.probe((mirror / "m.onnx").as_uri(), model)  # the pinned size: accepted


def test_probe_cli_fails_with_one_annotated_error_in_ci(fm, served, monkeypatch, capsys):
    model, mirror, _ = served
    monkeypatch.setattr(fm, "MODELS", {"m.onnx": model})
    monkeypatch.delenv("HEYP_MODELS_MIRROR", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(SystemExit) as info:
        fm.main(["--probe"])
    assert "1 model(s) cannot be downloaded" in str(info.value) and "publish_models.sh" in str(info.value)
    errors = [line for line in capsys.readouterr().out.splitlines() if line.startswith("::error")]
    assert len(errors) == 1 and "DEPLOY.md §7.1" in errors[0]
    monkeypatch.setenv("HEYP_MODELS_MIRROR", mirror.as_uri())
    fm.main(["--probe"])  # a mirror is enough


def test_fetch_failure_points_at_the_release_bootstrap(fm, served, tmp_path, monkeypatch):
    model, _, release = served
    monkeypatch.setattr(fm, "RELEASE", release)
    dest = tmp_path / "models"
    dest.mkdir()
    with pytest.raises(SystemExit, match="scripts/publish_models.sh"):
        fm.fetch("m.onnx", model, str(dest), urls=list(model.urls))
    with pytest.raises(SystemExit) as info:  # a failure unrelated to the release gets no such hint
        fm.fetch("m.onnx", model, str(dest), urls=["file:///nonexistent/m.onnx"])
    assert "publish_models.sh" not in str(info.value)


def test_list_prints_the_selected_names(fm, capsys):
    fm.main(["--list"])
    required = capsys.readouterr().out.split()
    fm.main(["--list", "--with-optional"])
    assert required == list(fm.selected()) and capsys.readouterr().out.split() == list(fm.MODELS)


# --- scripts/publish_models.sh -----------------------------------------------------------------------------------

def test_script_is_executable_bash_and_targets_the_pinned_release(fm):
    assert os.access(SCRIPT, os.X_OK) and SCRIPT.read_text().startswith("#!/usr/bin/env bash\n")
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode == 0
    text = SCRIPT.read_text()
    repo, tag = re.search(r'^REPO="([^"]+)"', text, re.M)[1], re.search(r'^TAG="([^"]+)"', text, re.M)[1]
    assert fm.RELEASE == f"https://github.com/{repo}/releases/download/{tag}"
    assert not re.search(r"^\s*mapfile\b", text, re.M)  # macOS ships bash 3.2


def _run(tmp_path: Path, *args: str, exists: bool = False) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run the script with a fake ``gh`` that logs its arguments (and says whether the release exists)."""
    bin_dir, log = tmp_path / "bin", tmp_path / "gh.log"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >>"$GH_LOG"\n'
                  f'[[ "$1 $2" == "release view" ]] && exit {0 if exists else 1}\nexit 0\n')
    gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "GH_LOG": str(log),
           "PYTHON": sys.executable}
    res = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=300)
    return res, log.read_text().splitlines() if log.exists() else []


def test_script_uploads_nothing_unless_every_file_matches_its_pin(tmp_path, fm):
    models = tmp_path / "models"
    models.mkdir()
    for name in fm.MODELS:
        (models / name).write_bytes(b"not the model")
    res, calls = _run(tmp_path, str(models))
    assert res.returncode != 0 and "MISSING OR MODIFIED" in res.stdout and calls == []
    res, calls = _run(tmp_path)
    assert res.returncode == 2 and "usage:" in res.stderr and calls == []


def _installed() -> Path:
    return Path(os.environ.get("HEYP_MODELS_DIR", "/opt/heyp/models"))


def _have(names) -> bool:
    return all((_installed() / name).is_file() for name in names)


def test_script_creates_the_release_with_every_pinned_file(tmp_path, fm):
    """Against the real installed models. Images ship only the required ones (the dev image always has them, so
    this runs in CI); the default run, which also uploads the optional variants, is checked where those exist."""
    if not _have(fm.selected()):
        pytest.skip("needs the required pinned models in HEYP_MODELS_DIR")
    every = _have(fm.MODELS)
    only = () if every else ("--required-only",)
    res, calls = _run(tmp_path, str(_installed()), "--target", "abc123", "--dry-run", *only)
    assert res.returncode == 0 and calls == [], res.stderr
    assert "release create models-v1 --repo uroy80/heyp" in res.stdout and "--target abc123" in res.stdout
    res, calls = _run(tmp_path, str(_installed()), *only, exists=True)
    assert res.returncode != 0 and "already exists" in res.stderr
    assert calls == ["release view models-v1 --repo uroy80/heyp"]  # never re-uploads into an existing release
    (tmp_path / "gh.log").unlink()
    res, calls = _run(tmp_path, str(_installed()), "--repo", "someone/fork", "--required-only")
    assert res.returncode == 0 and "published models-v1 in someone/fork" in res.stdout, res.stderr
    assert len(calls) == 2 and calls[0] == "release view models-v1 --repo someone/fork"
    assert calls[1].startswith("release create models-v1 --repo someone/fork --title Models v1 --notes ")
    uploaded = re.findall(r"(\S+\.onnx)\b", calls[1])
    assert [Path(p).name for p in uploaded] == list(fm.selected())
    if not every:
        return
    res, calls = _run(tmp_path, str(_installed()))  # the default: optional models too (immutable releases)
    uploaded = re.findall(r"(\S+\.onnx)\b", calls[-1])
    assert res.returncode == 0 and [Path(p).name for p in uploaded] == list(fm.MODELS)
