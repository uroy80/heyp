# heyp app image (ghcr.io/uroy80/heyp). Targets: pybase → models → base → dev (tests; source bind-mounted) |
# runtime (default, last). The renderer and egress images are built from services/*/Dockerfile.
#
# The base image is pinned by its multi-arch index digest so every build gets the same OS and Python; the literal
# FROM line lets Dependabot propose digest bumps. To pick up Debian/Python security fixes by hand:
# `scripts/lock.sh --base` prints the current digest of python:3.12-slim-trixie; paste it below, rebuild, and run
# scripts/smoke.sh.
FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS pybase

# --- models: ONNX weights, verified against pinned size + sha256 (any mismatch fails the build) ---------
FROM pybase AS models
# Optional comma-separated base URLs tried before the built-in sources (e.g. an internal mirror).
ARG HEYP_MODELS_MIRROR=
COPY scripts/fetch_models.py /tmp/fetch_models.py
RUN HEYP_MODELS_MIRROR="$HEYP_MODELS_MIRROR" python /tmp/fetch_models.py /models

# --- base: runtime deps from the fully pinned, hash-checked lock -------------------------------------
FROM pybase AS base
# ORT_DISABLE_TELEMETRY: no onnxruntime telemetry (privacy); it would also try to write a device id.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    ORT_DISABLE_TELEMETRY=1 \
    HEYP_MODELS_DIR=/opt/heyp/models
WORKDIR /app
# gifsicle (GPL-2.0) and pngquant (GPL-3.0) run as separate programs: animated-GIF and PNG compression.
# libfribidi0 (LGPL-2.1): Pillow's bundled raqm loads it at run time to shape Arabic, Hebrew and Indic text
# (without it text.py falls back to unshaped glyphs and warns).
RUN apt-get update \
 && apt-get install -y --no-install-recommends gifsicle pngquant libfribidi0 \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.lock .
# numpy's own test suite (~16 MB) is never imported; removing it in the same layer keeps it out of the image.
RUN pip install --require-hashes --no-deps -r requirements.lock \
 && pip check \
 && find "$(python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')/numpy" \
      -depth -type d -name tests -exec rm -rf {} +
COPY --from=models /models /opt/heyp/models
# Fail the build if the locked onnxruntime / OpenCV cannot load the pinned models.
RUN python -c "import cv2, onnxruntime as ort; m = '/opt/heyp/models/'; \
ort.InferenceSession(m + 'u2net_human_seg.onnx', providers=['CPUExecutionProvider']); \
cv2.FaceDetectorYN.create(m + 'face_detection_yunet_2023mar.onnx', '', (320, 320)); print('models load')"

# --- dev: test tooling; source is bind-mounted at /app -----------------------------------------------
FROM base AS dev
# Model-dependent tests fail (instead of skipping) when a model is missing or broken.
ENV HEYP_REQUIRE_MODELS=1
COPY requirements-dev.lock .
RUN pip install --require-hashes --no-deps -r requirements-dev.lock && pip check

# --- runtime ---------------------------------------------------------------------------------------
FROM base AS runtime
# Set by CI (release.yml): the version and the commit the image was built from. /api/health reports the
# revision; deploy/heyp-deploy refuses a container whose org.opencontainers.image.revision label is not the
# commit it deployed; /privacy and /terms link the source of exactly this commit (AGPL-3.0 §13).
ARG HEYP_VERSION=dev
ARG HEYP_REVISION=
LABEL org.opencontainers.image.title="heyp" \
      org.opencontainers.image.description="heyp — image toolbox: convert, resize, compress, edit, passport photos" \
      org.opencontainers.image.source="https://github.com/uroy80/heyp" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.version="${HEYP_VERSION}" \
      org.opencontainers.image.revision="${HEYP_REVISION}"
# MALLOC_ARENA_MAX=2: glibc keeps fewer per-thread arenas, so memory freed after a big job goes back to the
# OS (measured in a 3 GB container with three 48 MP jobs: peak 2.9 → 2.5 GB, idle 2.3 → 1.4 GB).
ENV MALLOC_ARENA_MAX=2 \
    HEYP_REVISION=${HEYP_REVISION}
# pip is only needed to build the image: remove it from what runs (smaller attack surface, fewer scanner hits).
# /run/renderer (the renderer socket volume) is created owned by the app user, so a fresh named volume inherits
# that ownership.
RUN python -m pip uninstall --yes --quiet pip \
 && useradd --create-home --uid 10001 heyp \
 && install -d -o heyp -g heyp -m 0755 /run/renderer
# heyp-serve: uvicorn with the deployment's proxy and logging settings.
COPY --chmod=0755 <<'EOF' /usr/local/bin/heyp-serve
#!/bin/sh
# Starts the heyp app. Only HEYP_TRUSTED_PROXIES may set the client address from X-Forwarded-For (never "*",
# which would let anyone spoof the address rate limits are keyed on); hosted mode keeps no access log.
set -eu
proxies=$(printf '%s' "${HEYP_TRUSTED_PROXIES:-127.0.0.1/32,::1/128}" | tr ' \t' ',,' | tr -s ',')
proxies=${proxies#,}
proxies=${proxies%,}
case "$proxies" in
  '' | *'*'*) echo "heyp-serve: HEYP_TRUSTED_PROXIES must list proxy addresses or networks, not '*'" >&2; exit 64 ;;
esac
set -- app.main:app --host 0.0.0.0 --port 8080 --proxy-headers --forwarded-allow-ips "$proxies" \
  --no-server-header --timeout-graceful-shutdown 25
if [ "${HEYP_MODE:-local}" = hosted ]; then
  set -- "$@" --no-access-log
fi
exec uvicorn "$@"
EOF
COPY app ./app
COPY static ./static
COPY legal ./legal
COPY CREDITS.md ./CREDITS.md
COPY LICENSE ./LICENSE
USER heyp
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --start-interval=2s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)" || exit 1
CMD ["/usr/local/bin/heyp-serve"]
