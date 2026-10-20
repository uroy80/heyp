"""Delivery regressions: compose defaults, build context, locked dependencies and dev-image settings."""
from __future__ import annotations

import os
import re
import subprocess
from importlib import metadata
from pathlib import Path

import pytest
from packaging.markers import Marker
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
UNITS = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}
# Measured with the compose limits (50 MP JPEG, two concurrent background removals = the worst op): the
# process with both ONNX models loaded takes ~0.45 GB, each running job ~30 bytes per output pixel.
BASELINE_BYTES = 450 << 20
JOB_BYTES_PER_PIXEL = 30


def _read(name: str) -> str:
    return (ROOT / name).read_text()


def _compose_default(var: str) -> str:
    """The ``${VAR:-default}`` default used in docker-compose.yml."""
    m = re.search(rf"\$\{{{var}:-([^}}]*)\}}", _read("docker-compose.yml"))
    assert m, f"docker-compose.yml does not use ${{{var}:-…}}"
    return m[1]


def _bytes(size: str) -> int:
    m = re.fullmatch(r"(\d+)([kmg]?)b?", size.strip().lower())
    assert m, size
    return int(m[1]) * UNITS.get(m[2], 1)


def _lock(name: str) -> dict[str, tuple[str, str | None, list[str]]]:
    """{name: (version, marker, hashes)} from a uv/pip-compile style hashed lock file."""
    entries: dict[str, tuple[str, str | None, list[str]]] = {}
    for block in re.split(r"\n(?=[A-Za-z0-9])", _read(name)):
        m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)\s*(?:;\s*([^\\\n]+?))?\s*\\", block)
        if m:
            entries[canonicalize_name(m[1])] = (m[2], m[3], re.findall(r"--hash=sha256:([0-9a-f]{64})", block))
    return entries


def _top_level(name: str) -> dict[str, str]:
    pins = {}
    for line in _read(name).splitlines():
        m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s;#]+)", line)
        if m:
            pins[canonicalize_name(m[1])] = m[2]
    return pins


def _dockerfile_stage(stage: str) -> str:
    m = re.search(rf"^FROM \S+ AS {stage}\n(.*?)(?=^FROM |\Z)", _read("Dockerfile"), re.M | re.S)
    assert m, f"no '{stage}' stage"
    return m[1]


# --- compose (DEL-1, DEL-2) ----------------------------------------------------------------------------

def test_compose_publishes_on_loopback_by_default():
    compose = _read("docker-compose.yml")
    ports = re.findall(r'^\s*-\s*"([^"]*:8080)"\s*$', compose, re.M)
    assert ports == ["${HEYP_BIND:-127.0.0.1}:${HEYP_PORT:-8080}:8080"]


@pytest.mark.parametrize("var, default", [("HEYP_ALLOW_CROSS_ORIGIN", "0"), ("HEYP_DOCS", "0"),
                                          ("HEYP_CACHE_MB", "512"), ("HEYP_MAX_PIXELS", "50000000"),
                                          ("HEYP_MAX_JOBS", "2")])
def test_compose_passes_hardening_settings_through(var, default):
    assert re.search(rf'^\s+{var}: "\$\{{{var}:-{default}\}}"$', _read("docker-compose.yml"), re.M)


def test_compose_bounds_memory_consistently_with_upload_limits():
    mem = _bytes(_compose_default("HEYP_MEM_LIMIT"))
    tmpfs = _bytes(_compose_default("HEYP_TMP_SIZE"))
    upload = int(_compose_default("HEYP_MAX_UPLOAD_MB")) << 20
    cache = int(_compose_default("HEYP_CACHE_MB")) << 20
    jobs = int(_compose_default("HEYP_MAX_JOBS"))
    pixels = int(_compose_default("HEYP_MAX_PIXELS"))
    assert re.search(r'^\s+mem_limit: "\$\{HEYP_MEM_LIMIT:-', _read("docker-compose.yml"), re.M)
    assert re.search(r'^\s+- "/tmp:size=\$\{HEYP_TMP_SIZE:-', _read("docker-compose.yml"), re.M)
    assert tmpfs >= 4 * upload  # a few max-size uploads plus their multipart spool fit
    # /tmp is RAM-backed and charged to the same limit; with the cache it may use at most half of it,
    # leaving the rest for the baseline (models loaded) and the running jobs.
    assert tmpfs + cache <= mem // 2
    assert 1 <= jobs <= 4
    # Every job slot busy with a max-size photo, plus a full image cache, must fit under the limit (API-2).
    assert BASELINE_BYTES + cache + jobs * pixels * JOB_BYTES_PER_PIXEL <= mem


# --- build context (DEL-8) -------------------------------------------------------------------------------

def test_build_context_is_an_allowlist_without_dotfiles():
    rules = [r.strip() for r in _read(".dockerignore").splitlines() if r.strip() and not r.startswith("#")]
    assert rules[0] == "*"
    assert "**/.*" in rules
    last_include = max(i for i, r in enumerate(rules) if r.startswith("!"))
    assert rules.index("**/.*") > last_include  # later rules win: dotfiles stay out of re-included dirs
    includes = {r[1:].rstrip("/") for r in rules if r.startswith("!")}
    sources = re.findall(r"^COPY (?!--from)(\S+) ", _read("Dockerfile"), re.M)
    assert sources and set(sources) <= includes, f"COPY sources missing from .dockerignore: {set(sources) - includes}"
    assert "static/tests/" in rules  # the UI unit tests are not part of the served UI


# --- dependencies and base image (DEL-4, DEL-5) -------------------------------------------------------------

def test_base_image_is_pinned_by_digest():
    dockerfile = _read("Dockerfile")
    m = re.search(r"^ARG PYTHON_IMAGE=(\S+)$", dockerfile, re.M)
    assert m and re.fullmatch(r"python:3\.12\.\d+-slim-[a-z]+@sha256:[0-9a-f]{64}", m[1])
    assert set(re.findall(r"^FROM (\S+)", dockerfile, re.M)) == {"${PYTHON_IMAGE}", "base"}
    assert not re.search(r"^# syntax=(?!.*@sha256:)", dockerfile, re.M)  # no floating frontend either


def test_images_install_only_from_hash_checked_locks():
    dockerfile = _read("Dockerfile")
    installs = re.findall(r"pip install (.+?)\s*(?:\\|&&|$)", dockerfile, re.M)
    assert installs == ["--require-hashes --no-deps -r requirements.lock",
                        "--require-hashes --no-deps -r requirements-dev.lock"]
    assert dockerfile.count("pip check") == 2


@pytest.mark.parametrize("lock", ["requirements.lock", "requirements-dev.lock"])
def test_lock_pins_and_hashes_every_package(lock):
    entries = _lock(lock)
    assert len(entries) >= 20
    for name, (version, _, hashes) in entries.items():
        assert re.fullmatch(r"[0-9][0-9A-Za-z.+!-]*", version), name
        assert hashes, f"{lock}: {name} has no --hash"
    declared = len(re.findall(r"^[A-Za-z0-9]", _read(lock), re.M))
    assert declared == len(entries)  # every requirement line parsed


def test_locks_agree_with_top_level_requirements():
    runtime, dev = _lock("requirements.lock"), _lock("requirements-dev.lock")
    for name, version in _top_level("requirements.txt").items():
        assert runtime[name][0] == version, name
    for name, version in _top_level("requirements-dev.txt").items():
        assert dev[name][0] == version, name
    for name, (version, _, _) in runtime.items():
        assert dev[name][0] == version, f"{name}: runtime {version} vs dev {dev[name][0]}"


def test_runtime_keeps_fast_server_extras_but_not_dev_ones():
    runtime = _lock("requirements.lock")
    assert {"uvicorn", "uvloop", "httptools"} <= runtime.keys()
    assert not {"watchfiles", "websockets", "pyyaml", "python-dotenv", "pytest", "httpx"} & runtime.keys()


def test_installed_packages_match_the_lock():
    """The image running these tests was built from the current locks (rebuild heyp:dev if not)."""
    for name, (version, marker, _) in _lock("requirements-dev.lock").items():
        if marker and not Marker(marker).evaluate():
            continue
        assert metadata.version(name) == version, f"{name}: installed {metadata.version(name)}, locked {version}"


# --- dev image + smoke script (DEL-7) ----------------------------------------------------------------------

def test_dev_image_requires_models():
    assert re.search(r"^ENV HEYP_REQUIRE_MODELS=1$", _dockerfile_stage("dev"), re.M)
    assert "HEYP_REQUIRE_MODELS" not in _dockerfile_stage("runtime")


def test_smoke_script_is_executable_bash():
    script = ROOT / "scripts" / "smoke.sh"
    assert os.access(script, os.X_OK)
    res = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_glibc_arenas_are_limited():
    """Freed job memory must go back to the OS between jobs (API-2 measurement: idle 2.3 → 1.4 GB)."""
    assert re.search(r"^ENV MALLOC_ARENA_MAX=2$", _dockerfile_stage("runtime"), re.M)
