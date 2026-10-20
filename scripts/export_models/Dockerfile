# One-off model exporter (.pth / .safetensors -> .onnx). NOT part of the heyp image.
#   docker build -t heyp-export-models scripts/export_models
#   scripts/export_models/export_all.sh            (see README.md)
#
# torch/torchvision come from the PyTorch CPU index on purpose: on linux/aarch64 the PyPI torchvision 0.29.1
# wheel is a CUDA 13 build (and pulls ~3 GB of CUDA libraries). Versions are pinned: other versions may
# export functionally equal but byte-different files, which would then not match scripts/fetch_models.py.
FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore PYTHONDONTWRITEBYTECODE=1
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.14.1+cpu torchvision==0.29.1+cpu \
 && pip install spandrel==0.4.2 onnx==1.23.2 onnxscript==0.7.2 onnxruntime==1.31.0 numpy==2.5.4 pillow==12.3.0 \
      safetensors==0.8.0 transformers==5.17.0 timm==1.0.30 einops==0.8.2 kornia==0.8.3
# export_all.sh runs it as the calling user (--user), so the exported files belong to you.
ENV HOME=/tmp
WORKDIR /work
