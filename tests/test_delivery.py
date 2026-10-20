"""Delivery regressions: compose files, build context, Dockerfile, locks, CI workflows and deploy scripts.

Everything here is static (no Docker, no network): the files are parsed with regexes, because the dev image has no
YAML parser. The behaviour itself is exercised by scripts/smoke.sh, .github/scripts/smoke-image.sh and
.github/scripts/stack-smoke.sh in CI.
"""
from __future__ import annotations

import json
import os
import re
import stat
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
IMAGES = {"heyp", "heyp-renderer", "heyp-egress"}
# The production server: 4 vCPU / 15 GB, shared with heypdf and other sites. heyp's ceilings stay within 6 GB.
SERVER_CPUS, HEYP_SHARE_BYTES = 4.0, 6 << 30
SHA_PIN = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def _read(name: str) -> str:
    return (ROOT / name).read_text()


def _compose_default(var: str, name: str = "docker-compose.yml") -> str:
    """The ``${VAR:-default}`` default used in a compose file."""
    m = re.search(rf"\$\{{{var}:-([^}}]*)\}}", _read(name))
    assert m, f"{name} does not use ${{{var}:-…}}"
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


def _dockerfile_stage(stage: str, name: str = "Dockerfile") -> str:
    m = re.search(rf"^FROM \S+ AS {stage}\n(.*?)(?=^FROM |\Z)", _read(name), re.M | re.S)
    assert m, f"no '{stage}' stage"
    return m[1]


def _instructions(text: str) -> list[str]:
    """Dockerfile instructions with line continuations joined (heredoc bodies are kept as written)."""
    return [re.sub(r"\\\n\s*", " ", block) for block in re.split(r"\n(?=[A-Z]+\s)", text)]


def _blocks(text: str, indent: int) -> dict[str, str]:
    """Top-level (indent 0) or nested (indent 2) YAML mapping keys → their raw text, for simple files."""
    pad = " " * indent
    out: dict[str, str] = {}
    line = rf"(?:{pad}  [^\n]*\n?|[ \t]*\n|{pad}#[^\n]*\n?)"  # deeper-indented, blank or comment
    for m in re.finditer(rf"^{pad}([A-Za-z0-9_.-]+):[^\n]*\n({line}*)", text, re.M):
        out[m[1]] = m[0]
    return out


def _services(name: str) -> dict[str, str]:
    return _blocks(_blocks(_read(name), 0)["services"], 2)


def _list(block: str, key: str) -> list[str]:
    m = re.search(rf"^\s+{key}: \[([^\]]*)\]", block, re.M)
    assert m, f"no inline '{key}: [...]' in\n{block[:200]}"
    return [x.strip() for x in m[1].split(",") if x.strip()]


# --- compose (DEL-1, DEL-2) ----------------------------------------------------------------------------

def test_compose_publishes_on_loopback_by_default():
    compose = _read("docker-compose.yml")
    ports = re.findall(r'^\s*-\s*"([^"]*:8080)"\s*$', compose, re.M)
    assert ports == ["${HEYP_BIND:-127.0.0.1}:${HEYP_PORT:-8080}:8080"]


@pytest.mark.parametrize("var, default", [("HEYP_ALLOW_CROSS_ORIGIN", "0"), ("HEYP_DOCS", "0"),
                                          ("HEYP_CACHE_MB", "512"), ("HEYP_MAX_PIXELS", "50000000"),
                                          ("HEYP_MAX_JOBS", "2"), ("HEYP_MODE", "local")])
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


def test_local_compose_keeps_optional_services_behind_profiles_and_isolated():
    services = _services("docker-compose.yml")
    assert set(services) == {"heyp", "renderer", "egress"}
    assert "profiles" not in services["heyp"]
    assert _list(services["renderer"], "profiles") == _list(services["egress"], "profiles") == ["render"]
    assert _list(services["renderer"], "networks") == ["render-sbx"]
    assert set(_list(services["egress"], "networks")) == {"render-sbx", "egress-wan"}
    assert "networks:" not in services["heyp"]  # the default network only: no route to the renderer or out
    for name, block in services.items():
        assert re.search(r"^\s+read_only: true$", block, re.M), name
        assert re.search(r"^\s+cap_drop: \[ALL\]$", block, re.M), name
        assert "no-new-privileges:true" in block, name
        assert re.search(r"^\s+mem_limit: ", block, re.M), name
    assert "seccomp=./deploy/seccomp/chromium-userns.json" in services["renderer"]


# --- production compose -----------------------------------------------------------------------------------------

PROD = "deploy/compose.prod.yml"


def test_prod_services_are_hardened_and_bounded():
    text = _read(PROD)
    anchors = _blocks(text, 0)
    hardening = anchors["x-hardening"]
    for line in ("read_only: true", "cap_drop: [ALL]", "no-new-privileges:true", "logging: *logging", "init: true"):
        assert line in hardening, line
    logging = anchors["x-logging"]
    assert "max-size:" in logging and "max-file:" in logging  # container logs are bounded (legal/privacy.md)
    services = _services(PROD)
    assert set(services) == {"app", "renderer", "egress"}
    for name in services:
        block = services[name]
        assert "<<: *hardening" in block, name
        for key in ("mem_limit", "cpus", "pids_limit"):
            assert re.search(rf"^\s+{key}: ", block, re.M), f"{name} has no {key}"
        image = re.search(r"^\s+image: (.+)$", block, re.M)[1]
        assert image == f"${{HEYP_REGISTRY:-ghcr.io/uroy80}}/{'heyp' if name == 'app' else 'heyp-' + name}" \
                        ":${HEYP_TAG:?HEYP_TAG is set by heyp-deploy}", image
    assert "profiles" not in services["app"]
    assert _list(services["renderer"], "profiles") == _list(services["egress"], "profiles") == ["render"]


def _limit(block: str, key: str) -> str:
    m = re.search(rf"^\s+{key}: \"?(?:\$\{{[A-Z_]+:-)?([0-9.]+[kmg]?)\}}?\"?$", block, re.M)
    assert m, f"{key} in\n{block[:300]}"
    return m[1]


def test_prod_default_limits_fit_heyps_share_of_the_server():
    services = _services(PROD)
    total = sum(_bytes(_limit(services[n], "mem_limit")) for n in services)
    assert total <= HEYP_SHARE_BYTES, f"{total / (1 << 30):.2f} GiB of limits"
    for name, block in services.items():
        cpus = float(_limit(block, "cpus"))
        assert 0 < cpus < SERVER_CPUS / 2, f"{name} may take half the server"  # heypdf and the other sites keep CPU
    # The app's tmpfs (uploads live in RAM, charged to its limit) leaves room for the models and a heavy job.
    app = services["app"]
    tmp = _bytes(re.search(r'"/tmp:size=\$\{HEYP_TMP_SIZE:-([0-9]+[kmg])\}"', app)[1])
    assert tmp <= _bytes(_limit(app, "mem_limit")) // 4


def test_prod_networks_isolate_the_renderer():
    services = _services(PROD)
    assert _list(services["renderer"], "networks") == ["render-sbx"]
    assert set(_list(services["egress"], "networks")) == {"render-sbx", "egress-wan"}
    assert _list(services["app"], "networks") == ["edge"]
    networks = _blocks(_blocks(_read(PROD), 0)["networks"], 2)
    assert set(networks) == {"edge", "render-sbx", "egress-wan"}
    assert "internal: true" in networks["render-sbx"]
    assert "com.docker.network.bridge.gateway_mode_ipv4: isolated" in networks["render-sbx"]
    # app ↔ renderer only through the socket volume; the renderer's seccomp profile ships with deploy/.
    assert "render-sock:/run/renderer" in services["app"] and "render-sock:/run/renderer" in services["renderer"]
    assert "seccomp=./seccomp/chromium-userns.json" in services["renderer"]


def test_prod_app_settings_match_the_contract():
    app = _services(PROD)["app"]
    assert "HEYP_MODE: hosted" in app
    proxies = re.search(r'HEYP_TRUSTED_PROXIES: "([^"]+)"', app)[1]
    assert "*" not in proxies and re.search(r"\d+\.\d+\.\d+\.\d+/\d+", proxies)
    assert 'HEYP_SIBLING_URL: "${HEYP_SIBLING_URL:-https://heypdf.edgr.in}"' in app  # heypdf is a plain link
    assert "volumes:\n      - render-sock:/run/renderer\n" in app  # no persistent state at all
    assert "volumes:\n  render-sock: {}\n" in _read(PROD)
    egress = _services(PROD)["egress"]
    assert re.search(r"HEYP_EGRESS_DENY_CIDRS: \"\$\{HEYP_EGRESS_DENY_CIDRS:\?", egress)  # required, never empty
    for name, block in _services(PROD).items():
        for var in re.findall(r"^\s+([A-Z][A-Z0-9_]*):", block, re.M):
            assert var.startswith("HEYP_") or var == "HOME", f"{name}: {var}"


def test_env_example_documents_every_required_setting():
    example = _read("deploy/heyp.env.example")
    required = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*):\?", _read(PROD))) - {"HEYP_TAG"}
    assert required >= {"HEYP_DOMAIN", "HEYP_OPERATOR_NAME", "HEYP_OPERATOR_EMAIL", "HEYP_EGRESS_SECRET"}
    for var in required:
        assert re.search(rf"^#?{var}=", example, re.M), f"{var} is required by {PROD} but not in heyp.env.example"
    assert re.search(r"^HEYP_EGRESS_SECRET=$", example, re.M)  # no secret value in the example


def test_seccomp_profile_allows_exactly_the_chromium_sandbox_additions():
    profile = json.loads(_read("deploy/seccomp/chromium-userns.json"))
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    allowed = {n for rule in profile["syscalls"] if rule["action"] == "SCMP_ACT_ALLOW" for n in rule["names"]}
    assert {"clone", "unshare", "chroot"} <= allowed
    for copy in ROOT.glob("services/renderer/**/*.json"):
        if "seccomp" in copy.name or "seccomp" in copy.parent.name:
            assert json.loads(copy.read_text()) == profile, f"{copy} differs from deploy/seccomp/"


# --- build context (DEL-8) -------------------------------------------------------------------------------

def _copy_sources(dockerfile: str) -> list[str]:
    """Build-context sources of every COPY (flags skipped; --from stages and heredocs excluded)."""
    sources = []
    for flags, rest in re.findall(r"^COPY((?:\s+--\S+)*)\s+(.+)$", dockerfile, re.M):
        if "--from" in flags or rest.startswith("<<"):
            continue
        sources += rest.split()[:-1]
    return sources


def test_build_context_is_an_allowlist_without_dotfiles():
    rules = [r.strip() for r in _read(".dockerignore").splitlines() if r.strip() and not r.startswith("#")]
    assert rules[0] == "*"
    assert "**/.*" in rules
    last_include = max(i for i, r in enumerate(rules) if r.startswith("!"))
    assert rules.index("**/.*") > last_include  # later rules win: dotfiles stay out of re-included dirs
    includes = {r[1:].rstrip("/") for r in rules if r.startswith("!")}
    sources = _copy_sources(_read("Dockerfile"))
    assert sources and set(sources) <= includes, f"COPY sources missing from .dockerignore: {set(sources) - includes}"
    assert "static/tests/" in rules  # the UI unit tests are not part of the served UI
    assert {"legal", "CREDITS.md", "LICENSE"} <= includes  # /privacy, /terms, /credits are rendered from these


# --- dependencies and base image (DEL-4, DEL-5) -------------------------------------------------------------

def test_base_image_is_pinned_by_digest():
    dockerfile = _read("Dockerfile")
    froms = re.findall(r"^FROM (\S+) AS (\S+)$", dockerfile, re.M)
    assert len(froms) == len(re.findall(r"^FROM ", dockerfile, re.M)), "every stage is named"
    stages = [s for _, s in froms]
    assert stages == ["pybase", "models", "base", "dev", "runtime"]  # runtime is last: the default target
    external = [image for image, _ in froms if image not in stages]
    # One literal, digest-pinned base (Dependabot can only update a literal FROM line).
    assert len(external) == 1 and re.fullmatch(r"python:3\.12\.\d+-slim-[a-z]+@sha256:[0-9a-f]{64}", external[0])
    assert not re.search(r"^# syntax=(?!.*@sha256:)", dockerfile, re.M)  # no floating frontend either


@pytest.mark.parametrize("dockerfile", sorted(str(p.relative_to(ROOT)) for p in ROOT.glob("services/*/Dockerfile")))
def test_service_images_pin_their_base_by_digest(dockerfile):
    text = _read(dockerfile)
    args = dict(re.findall(r"^ARG (\w+)=(\S+)$", text, re.M))
    stages = set(re.findall(r"^FROM \S+ AS (\S+)", text, re.M | re.I))
    for image in re.findall(r"^FROM (\S+)", text, re.M):
        image = re.sub(r"\$\{(\w+)\}", lambda m: args.get(m[1], m[0]), image)
        if image in stages:
            continue
        assert re.search(r"@sha256:[0-9a-f]{64}$", image), f"{dockerfile}: FROM {image} is not pinned by digest"


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


def test_lock_script_reads_the_pinned_base():
    script = _read("scripts/lock.sh")
    base = re.search(r"^FROM (python:\S+@sha256:[0-9a-f]{64}) AS pybase$", _read("Dockerfile"), re.M)[1]
    sed = re.search(r"sed -n '(s/[^']+/p)' Dockerfile", script)[1]
    out = subprocess.run(["sed", "-n", sed], input=_read("Dockerfile"), capture_output=True, text=True, check=True)
    assert out.stdout.strip() == base


# --- dev image, runtime image, smoke script (DEL-7) ----------------------------------------------------------------

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
    envs = [i for i in _instructions(_dockerfile_stage("runtime")) if i.startswith("ENV ")]
    assert any(re.search(r"\bMALLOC_ARENA_MAX=2\b", i) for i in envs)


def test_runtime_image_ships_the_legal_sources_and_its_revision():
    runtime = _dockerfile_stage("runtime")
    for src in ("legal", "CREDITS.md", "LICENSE", "app", "static"):
        assert re.search(rf"^COPY {src} ", runtime, re.M), src
    assert 'org.opencontainers.image.revision="${HEYP_REVISION}"' in runtime  # heyp-deploy checks this label
    assert 'org.opencontainers.image.licenses="AGPL-3.0-or-later"' in runtime
    assert re.search(r"^USER heyp$", runtime, re.M) and re.search(r'^CMD \["/usr/local/bin/heyp-serve"\]$', runtime, re.M)
    assert "pip uninstall" in runtime  # pip is not shipped in the runtime image


def _heyp_serve(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    body = re.search(r"^COPY --chmod=0755 <<'EOF' /usr/local/bin/heyp-serve\n(.*?)^EOF$", _read("Dockerfile"),
                     re.M | re.S)[1]
    script = tmp_path / "heyp-serve"
    script.write_text(body)
    fake = tmp_path / "uvicorn"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return subprocess.run(["sh", str(script)], capture_output=True, text=True,
                          env={"PATH": f"{tmp_path}:/usr/bin:/bin", **env})


def test_heyp_serve_trusts_only_the_configured_proxies(tmp_path):
    default = _heyp_serve(tmp_path, {})
    assert default.returncode == 0, default.stderr
    args = default.stdout.split("\n")
    assert args[:2] == ["app.main:app", "--host"]
    assert args[args.index("--forwarded-allow-ips") + 1] == "127.0.0.1/32,::1/128"
    assert "--proxy-headers" in args and "--no-access-log" not in args
    listed = _heyp_serve(tmp_path, {"HEYP_TRUSTED_PROXIES": " 172.31.251.0/24  10.0.0.2/32,"}).stdout.split("\n")
    assert listed[listed.index("--forwarded-allow-ips") + 1] == "172.31.251.0/24,10.0.0.2/32"
    for bad in ("*", "10.0.0.1,*", " , "):
        res = _heyp_serve(tmp_path, {"HEYP_TRUSTED_PROXIES": bad})
        assert res.returncode == 64 and "not '*'" in res.stderr, bad


def test_hosted_mode_keeps_no_app_access_log(tmp_path):
    """legal/privacy.md: "The heyp app itself keeps no access log of its own here"."""
    res = _heyp_serve(tmp_path, {"HEYP_MODE": "hosted"})
    assert res.returncode == 0 and "--no-access-log" in res.stdout.split("\n")


# --- CI / release / deploy workflows ------------------------------------------------------------------------------

WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def test_every_workflow_is_least_privilege_and_pinned():
    assert {p.name for p in WORKFLOWS} >= {"ci.yml", "release.yml", "deploy.yml", "relock.yml"}
    for path in WORKFLOWS:
        text = path.read_text()
        assert re.search(r"^permissions: \{\}$", text, re.M), f"{path.name}: no top-level 'permissions: {{}}'"
        for uses in re.findall(r"^\s+(?:- )?uses: (\S+)", text, re.M):
            assert uses.startswith("./") or SHA_PIN.match(uses), f"{path.name}: {uses} is not pinned to a commit"
        checkouts = text.count("uses: actions/checkout@")
        assert text.count("persist-credentials: false") >= checkouts, f"{path.name}: checkout keeps credentials"
        assert "pull_request_target" not in text and "secrets: inherit" not in text, path.name


def _matrix_images(text: str) -> set[str]:
    return set(re.findall(r"\bimage: (heyp(?:-[a-z]+)?)\b", text)) | {
        i for m in re.findall(r"image: \[([^\]]+)\]", text) for i in re.split(r",\s*", m)}


def test_the_three_images_agree_everywhere():
    ci, release = _read(".github/workflows/ci.yml"), _read(".github/workflows/release.yml")
    assert _matrix_images(ci) == IMAGES
    assert _matrix_images(release) == IMAGES
    assert set(re.search(r"IMAGES: ([\w -]+?)\s*(?:#|$)", _read(".github/workflows/deploy.yml"), re.M)[1].split()) \
        == IMAGES
    prod = set(re.findall(r"/(heyp(?:-[a-z]+)?):\$\{HEYP_TAG", _read(PROD)))
    assert prod == IMAGES
    for image in IMAGES - {"heyp"}:
        assert f"context: services/{image.removeprefix('heyp-')}" in ci, image
        assert f"context: services/{image.removeprefix('heyp-')}" in release, image
    for svc in ("renderer", "egress"):
        assert f"tags: heyp-ci/heyp-{svc}:test" in ci  # the stack job builds every image it starts


def test_ci_runs_every_test_suite_and_the_full_stack():
    ci = _read(".github/workflows/ci.yml")
    assert "pytest -q -m \"not services\"" in ci
    assert "node --test static/tests/*.test.mjs" in ci and "node --check" in ci
    assert "ruff-action@" in ci and "args: check" in ci
    assert ".github/scripts/stack-smoke.sh" in ci and ".github/scripts/validate-deploy.sh" in ci
    assert "service: [renderer, egress]" in ci
    needs = re.search(r"ci-ok:.*?needs: \[([^\]]+)\]", ci, re.S)[1]
    jobs = set(re.findall(r"^  ([\w-]+):\n", ci.split("\njobs:\n", 1)[1], re.M)) - {"ci-ok"}
    assert {j.strip() for j in needs.split(",")} == jobs  # "CI OK" really covers every job
    ruff = _read("ruff.toml")
    assert 'select = ["E4", "E7", "E9", "F"]' in ruff and "line-length = 120" in ruff


SCRIPTS = ["scripts/smoke.sh", "scripts/lock.sh", "deploy/bootstrap-vps.sh", "deploy/heyp-deploy",
           "deploy/heyp-egress-guard", ".github/scripts/smoke-image.sh", ".github/scripts/stack-smoke.sh",
           ".github/scripts/validate-deploy.sh", ".github/scripts/service-tests.sh", "deploy/apache/refresh-cf-ips.sh"]


@pytest.mark.parametrize("script", SCRIPTS)
def test_delivery_scripts_are_executable_bash(script):
    path = ROOT / script
    assert os.access(path, os.X_OK), f"{script} is not executable"
    assert path.read_text().startswith("#!/usr/bin/env bash\n")
    res = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_deploy_wrapper_refuses_network_settings_from_ci():
    script = _read("deploy/heyp-deploy")
    refused = re.search(r"case \"\$\{line%%=\*\}\" in\n\s+#[^\n]*\n\s+([^)]+)\)", script)[1]
    for key in ("HEYP_TAG", "HEYP_REGISTRY", "HEYP_EGRESS_DENY_CIDRS", "HEYP_TRUSTED_PROXIES", "HEYP_BIND"):
        assert key in refused, key
    assert 'restrict,command="/usr/local/sbin/heyp-deploy"' in script


def test_deploy_docs_cover_the_operator_checklist():
    doc = _read("deploy/DEPLOY.md")
    for topic in ("Hostinger", "AlmaLinux", "4 vCPU", "DEPLOY_SSH_KEY", "DEPLOY_KNOWN_HOSTS", "DEPLOY_HOST", "HEYP_URL",
                  "HEYP_SIBLING_URL", "heypdf", "8180", "8181", "models-v1", "rollback", "HEYP_EGRESS_DENY_CIDRS",
                  "HEYP_OPERATOR_NAME", "swap", "HEYP_TRUSTED_PROXIES", "DNS", "PROD_ENV",
                  # Apache + Cloudflare on the server
                  "Apache", "deploy/apache/", "CF-Connecting-IP", "HEYP_CDN_PROVIDER", "Full (strict)",
                  "Email Address Obfuscation", "Rocket Loader", "Bot Fight Mode", "Zaraz", "Bypass cache", "httpd -S",
                  "apachectl -t", "--keep-ci-env", "deploy/heyp.env.example", "/var/log/httpd-heyp/",
                  "/etc/ssl/edgr/origin.pem"):
        assert topic in doc, f"deploy/DEPLOY.md does not mention {topic}"
    # Removing a value is "delete the PROD_ENV line and redeploy" — true only because heyp-deploy treats CI as
    # authoritative.
    assert "delete that line (or the whole `PROD_ENV` secret) and redeploy" in doc


# --- Apache + Cloudflare on the production server --------------------------------------------------------------

APACHE = ROOT / "deploy" / "apache"
ENV_EXAMPLE = "deploy/heyp.env.example"
TAKEN_PORTS = {22, 80, 443, 3000, 4416, 8100, 8181}  # other sites and heypdf on the production server


def _env_values(name: str) -> dict[str, str]:
    """KEY=VALUE lines of an env file (uncommented; surrounding double quotes removed, as compose does)."""
    out = {}
    for line in _read(name).splitlines():
        m = re.match(r"([A-Z][A-Z0-9_]*)=(.*)$", line)
        if m:
            value = m[2].strip()
            out[m[1]] = value[1:-1] if len(value) >= 2 and value[0] == value[-1] == '"' else value
    return out


def _vhosts(text: str) -> list[tuple[str, str]]:
    return re.findall(r"^<VirtualHost ([^>]+)>\n(.*?)^</VirtualHost>", text, re.M | re.S)


def _directives(block: str) -> list[str]:
    return [line.strip() for line in block.splitlines() if line.strip() and not line.strip().startswith("#")]


def test_cloudflare_ranges_are_public():
    import ipaddress

    ranges = re.findall(r"^RemoteIPTrustedProxy (\S+)$", (APACHE / "heyp-cloudflare-ips.inc").read_text(), re.M)
    assert len(ranges) >= 20
    for cidr in ranges:
        net = ipaddress.ip_network(cidr)  # strict: a typo with host bits set fails here
        assert net.is_global and net.prefixlen >= (8 if net.version == 4 else 16), cidr
    assert any(ipaddress.ip_network(c).version == 6 for c in ranges)
    # Nothing else in the Apache kit may vouch for a client address.
    for conf in ("heyp.conf", "ssl-heyp.conf"):
        directives = _directives((APACHE / conf).read_text())
        assert not any(d.startswith(("RemoteIPTrustedProxy", "RemoteIPInternalProxy")) for d in directives), conf


def test_apache_vhosts_restore_the_visitor_and_forward_exactly_one_hop():
    ssl, plain = (APACHE / "ssl-heyp.conf").read_text(), (APACHE / "heyp.conf").read_text()
    vhosts = _vhosts(ssl) + _vhosts(plain)
    assert len(vhosts) == 2
    for _, body in vhosts:  # every heyp vhost resolves the visitor from Cloudflare's header (logs + limits)
        directives = _directives(body)
        assert "RemoteIPHeader CF-Connecting-IP" in directives
        assert "Include /etc/httpd/conf.d/heyp-cloudflare-ips.inc" in directives
    [(addr, body)] = _vhosts(ssl)
    directives = _directives(body)
    assert addr == "*:443" and "ServerName heyp.edgr.in" in directives
    assert not any(d.startswith("ServerAlias") for d in directives)  # heypdf is its own site
    for needed in ("SSLEngine on", "SSLCertificateFile /etc/ssl/edgr/origin.pem",
                   "SSLCertificateKeyFile /etc/ssl/edgr/origin.key",
                   "RequestHeader unset X-Forwarded-For early",      # the app takes the right-most hop: only ours
                   "RequestHeader unset X-Forwarded-Host early", "RequestHeader unset Forwarded early",
                   "RequestHeader unset CF-Connecting-IP",           # a spoofed one never reaches the app
                   'RequestHeader set X-Forwarded-Proto "https"',    # https redirects
                   "ProxyPreserveHost On", "ProxyAddHeaders On", "ProxyRequests Off"):
        assert needed in directives, needed
    assert not re.search(r"^\s*Define ", ssl, re.M)  # a Define is global to Apache: it could clash with heypdf's
    passes = [d for d in directives if d.startswith("ProxyPass ")]
    assert len(passes) == 1
    m = re.fullmatch(r"ProxyPass\s+/ http://127\.0\.0\.1:(\d+)/ .*", passes[0])
    assert m and f"ProxyPassReverse / http://127.0.0.1:{m[1]}/" in directives
    assert int(re.search(r"\btimeout=(\d+)", passes[0])[1]) >= 120
    assert 'SetEnvIfNoCase Request_URI "/events$" no-gzip=1 no-brotli=1' in directives  # SSE is never buffered
    env = _env_values(ENV_EXAMPLE)
    assert env["HEYP_PORT"] == m[1] == "8180" and int(m[1]) not in TAKEN_PORTS
    assert env["HEYP_BIND"] == "127.0.0.1"
    limit = int(re.search(r"^LimitRequestBody (\d+)$", "\n".join(directives), re.M)[1])
    assert (int(env["HEYP_MAX_UPLOAD_MB"]) + 1) << 20 <= limit <= 100 << 20  # Cloudflare's per-request cap
    [(addr, body)] = _vhosts(plain)  # port 80: a redirect to the same name over HTTPS, nothing proxied
    assert addr == "*:80" and "ServerName heyp.edgr.in" in _directives(body)
    assert "Redirect permanent / https://heyp.edgr.in/" in body and "ProxyPass" not in body


def test_env_example_fits_the_target():
    env = _env_values(ENV_EXAMPLE)
    assert env["COMPOSE_PROFILES"] == "render"
    assert (env["HEYP_DOMAIN"], env["HEYP_SIBLING_URL"]) == ("heyp.edgr.in", "https://heypdf.edgr.in")
    assert (env["HEYP_OPERATOR_NAME"], env["HEYP_OPERATOR_EMAIL"]) == ("Usham Roy", "support@edgr.in")
    assert env["HEYP_GRIEVANCE_OFFICER"] == "Usham Roy" and env["HEYP_JURISDICTION"] == "India"
    assert env["HEYP_CDN_PROVIDER"] == "Cloudflare" and env["HEYP_CDN_DPA_URL"].startswith("https://")
    assert int(env["HEYP_MAX_UPLOAD_MB"]) < int(env["HEYP_MAX_REQUEST_MB"]) < 100
    assert "HEYP_TRUSTED_PROXIES" not in env  # the edge network default is the Apache hop (its gateway)
    required = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*):\?", _read(PROD))) - {"HEYP_TAG"}
    for var in required:
        assert var in env, f"{var} is required by {PROD} but not in {ENV_EXAMPLE}"
    # 4 vCPU / 15 GB shared with heypdf and other sites: heyp's ceilings stay within its share, no container takes
    # half the CPUs.
    total = 0
    for name, block in _services(PROD).items():
        m = re.search(r'^\s+mem_limit: "?(?:\$\{([A-Z_]+):-)?([0-9.]+[kmg]?)\}?"?$', block, re.M)
        total += _bytes(env.get(m[1], m[2]) if m[1] else m[2])
        c = re.search(r"^\s+cpus: \"?(?:\$\{([A-Z_]+):-)?([0-9.]+)\}?\"?$", block, re.M)
        cpus = float(env.get(c[1], c[2]) if c[1] else c[2])
        assert 0 < cpus <= SERVER_CPUS / 2, name
    assert total <= HEYP_SHARE_BYTES, f"{total / (1 << 30):.1f} GiB of limits"
    assert _bytes(env["HEYP_TMP_SIZE"]) <= _bytes(env["HEYP_MEM_LIMIT"]) // 4


def test_bootstrap_supports_almalinux_and_installs_the_one_env_example():
    script = _read("deploy/bootstrap-vps.sh")
    assert re.search(r"almalinux:9\* \| almalinux:10\*", script) and "rhel:10*" in script
    assert "dnf -y -q install git curl util-linux coreutils" in script
    assert re.search(r'command -v apt-get >/dev/null \|\| die "--ufw is for Ubuntu/Debian', script)
    assert "ENV_TEMPLATE=heyp.env.example" in script and '"$SRC/$ENV_TEMPLATE" /etc/heyp/heyp.env' in script
    assert "--env-template" not in script and "caddy" not in script.lower()
    # Another app's deploy key on the same user (heypdf's, with its own forced command) is not a warning.
    assert "grep -vq 'command=\"'" in script


def _read_ci_env_harness(tmp_path: Path) -> Path:
    body = re.search(r"^read_ci_env\(\) \{\n.*?^\}\n", _read("deploy/heyp-deploy"), re.M | re.S)[0]
    harness = tmp_path / "read_ci_env.sh"
    harness.write_text("set -Eeuo pipefail\nSTATE=$1\nLOG=$STATE/deploy.log\nKEEP_CI_ENV=${KEEP:-0}\n"
                       "log() { printf '%s\\n' \"$*\" >>\"$LOG\"; }\ndie() { log \"error: $1\"; exit \"${2:-1}\"; }\n"
                       + body + "read_ci_env\n")
    return harness


def test_deploy_wrapper_treats_the_ci_env_as_authoritative(tmp_path):
    """Deleting a value from PROD_ENV must remove it from the server (CI is authoritative for ci.env)."""
    harness = _read_ci_env_harness(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    ci_env = state / "ci.env"

    def deploy(stdin: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(harness), str(state)], input=stdin, capture_output=True, text=True,
                              env={"PATH": os.environ["PATH"], **env})

    assert deploy("HEYP_MAX_UPLOAD_MB=80\n").returncode == 0
    assert ci_env.read_text() == "HEYP_MAX_UPLOAD_MB=80\n"
    # What deploy.yml sends for an emptied or deleted secret: printf '%s\n' "" → one empty line.
    assert deploy("\n").returncode == 0
    assert not ci_env.exists()
    assert "ci.env cleared" in (state / "deploy.log").read_text()
    # A changed secret replaces the file as a whole (no stale keys survive).
    deploy("HEYP_MAX_UPLOAD_MB=80\nHEYP_TTL_MINUTES=45\n")
    deploy("HEYP_TTL_MINUTES=30\n")
    assert ci_env.read_text() == "HEYP_TTL_MINUTES=30\n"
    # The owner's explicit --keep-ci-env keeps it; a rejected line changes nothing.
    assert deploy("\n", KEEP="1").returncode == 0 and ci_env.read_text() == "HEYP_TTL_MINUTES=30\n"
    assert deploy("HEYP_TRUSTED_PROXIES=0.0.0.0/0\n").returncode == 64
    assert deploy("not an env line\n").returncode == 64
    assert deploy("HEYPDF_MODE=hosted\n").returncode == 64  # only HEYP_* keys: heypdf has its own server config
    assert ci_env.read_text() == "HEYP_TTL_MINUTES=30\n"
    assert not list(state.glob(".ci.env.*"))  # no temporary files left behind
    # --keep-ci-env is honoured only outside the CI key's forced command.
    script = _read("deploy/heyp-deploy")
    assert re.search(r'\[ "\$\{ARGS\[2\]\}" = --keep-ci-env \] && \[ -z "\$\{SSH_ORIGINAL_COMMAND:-\}" \]', script)


def test_deploy_wrapper_prunes_only_heyp_images():
    """OPS-13: a shared server's other projects keep their dangling images."""
    script = _read("deploy/heyp-deploy")
    prunes = re.findall(r"docker (?:image|system|builder) prune[^\n]*", script)
    assert prunes == ['docker image prune -f --filter "label=org.opencontainers.image.source=$IMAGE_SOURCE" '
                      ">/dev/null 2>&1 || true"]
    assert "IMAGE_SOURCE=${IMAGE_SOURCE:-${REPO_URL%.git}}" in script
    assert 'org.opencontainers.image.source="https://github.com/uroy80/heyp"' in _read("Dockerfile")
    assert "org.opencontainers.image.source=${{ github.server_url }}/${{ github.repository }}" in \
        _read(".github/workflows/release.yml")


def test_refresh_cf_ips_validates_before_it_installs(tmp_path):
    bin_dir, conf = tmp_path / "bin", tmp_path / "conf.d"
    bin_dir.mkdir()
    conf.mkdir()
    out = conf / "heyp-cloudflare-ips.inc"
    for name, body in (("curl", 'for a; do u=$a; done; cat "$FAKE/$(basename "$u")"'),
                       ("apachectl", 'echo apachectl >>"$FAKE/calls"; exit "${APACHECTL_RC:-0}"'),
                       ("systemctl", 'echo "systemctl $*" >>"$FAKE/calls"')):
        (bin_dir / name).write_text(f"#!/bin/sh\n{body}\n")
        (bin_dir / name).chmod(0o755)
    pinned = (APACHE / "heyp-cloudflare-ips.inc").read_text()
    v4 = "\n".join(c for c in re.findall(r"RemoteIPTrustedProxy (\S+)", pinned) if ":" not in c)  # no final newline
    v6 = "\n".join(c for c in re.findall(r"RemoteIPTrustedProxy (\S+)", pinned) if ":" in c) + "\n"

    def run(ips_v4: str, ips_v6: str = v6, **env: str) -> subprocess.CompletedProcess:
        (tmp_path / "ips-v4").write_text(ips_v4)
        (tmp_path / "ips-v6").write_text(ips_v6)
        (tmp_path / "calls").unlink(missing_ok=True)
        return subprocess.run(["bash", str(APACHE / "refresh-cf-ips.sh")], capture_output=True, text=True, env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE": str(tmp_path), "HEYP_CF_IPS_FILE": str(out),
            "HEYP_CF_IPS_URLS": "https://cf.invalid/ips-v4 https://cf.invalid/ips-v6", **env})

    res = run(v4)
    assert res.returncode == 0, res.stderr
    written = re.findall(r"^RemoteIPTrustedProxy (\S+)$", out.read_text(), re.M)
    assert written == re.findall(r"^RemoteIPTrustedProxy (\S+)$", pinned, re.M)
    assert (tmp_path / "calls").read_text() == "apachectl\nsystemctl reload httpd.service\n"
    assert "unchanged" in run(v4).stdout and not (tmp_path / "calls").exists()  # same list: no reload
    good = out.read_text()
    for bad in (v4 + "\n0.0.0.0/0", v4 + "\nInclude /etc/passwd", "173.245.48.0/20\n", v4 + "\n10.0.0.0/33"):
        res = run(bad)
        assert res.returncode == 1 and out.read_text() == good, bad
    # A list Apache rejects is rolled back.
    res = run(v4 + "\n198.51.100.0/24", APACHECTL_RC="1")
    assert res.returncode == 1 and out.read_text() == good and "systemctl" not in (tmp_path / "calls").read_text()


def test_deploy_workflow_checks_the_privacy_claims_on_the_public_site():
    deploy = _read(".github/workflows/deploy.yml")
    step = deploy.split("- name: Privacy claims hold on the public site", 1)[1].split("\n      - name:", 1)[0]
    assert "set-cookie" in step.lower() and "/cdn-cgi/" in step and 'exit "$ok"' in step
    for path in ("/ ", "/privacy", "/terms"):
        assert path in step
    # A CDN setting is not the release's fault: only the health check triggers a rollback.
    assert "if: failure() && steps.health.outcome == 'failure'" in deploy
