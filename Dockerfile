# Targets: models → base → dev (tests; source bind-mounted) | runtime (default, last).
#
# The base image is pinned by its multi-arch index digest so every build gets the same OS and Python.
# Pick up Debian/Python security fixes deliberately: `scripts/lock.sh --base` prints the current digest of
# python:3.12-slim-trixie; paste it below, rebuild, and run scripts/smoke.sh.
ARG PYTHON_IMAGE=python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

# --- models: ONNX weights, verified against pinned size + sha256 (any mismatch fails the build) ---------
FROM ${PYTHON_IMAGE} AS models
# Optional comma-separated base URLs tried before the built-in sources (e.g. an internal mirror).
ARG HEYP_MODELS_MIRROR=
COPY scripts/fetch_models.py /tmp/fetch_models.py
RUN HEYP_MODELS_MIRROR="$HEYP_MODELS_MIRROR" python /tmp/fetch_models.py /models

# --- base: runtime deps from the fully pinned, hash-checked lock -------------------------------------
FROM ${PYTHON_IMAGE} AS base
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
RUN apt-get update \
 && apt-get install -y --no-install-recommends gifsicle pngquant \
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
# MALLOC_ARENA_MAX=2: glibc keeps fewer per-thread arenas, so memory freed after a big job goes back to the
# OS (measured in a 3 GB container with three 48 MP jobs: peak 2.9 → 2.5 GB, idle 2.3 → 1.4 GB).
ENV MALLOC_ARENA_MAX=2
RUN useradd --create-home --uid 10001 heyp
COPY app ./app
COPY static ./static
USER heyp
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --start-interval=2s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
