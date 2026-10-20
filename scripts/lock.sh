#!/usr/bin/env bash
# Regenerate requirements.lock and requirements-dev.lock from requirements.txt / requirements-dev.txt.
#
#   scripts/lock.sh                 # re-lock after editing requirements*.txt (keeps existing pins)
#   scripts/lock.sh --upgrade       # bump every transitive pin to the newest allowed version
#   scripts/lock.sh --base          # only print the current digest of python:3.12-slim-trixie
#
# uv runs inside the pinned base image, so no local Python is needed. The locks are universal (valid for
# linux/amd64 and linux/arm64 builds) and carry sha256 hashes for every distribution; the Dockerfile
# installs them with `pip install --require-hashes --no-deps` followed by `pip check`.
set -euo pipefail
cd "$(dirname "$0")/.."

UV_VERSION=0.13.0
BASE_TAG=python:3.12-slim-trixie
BASE=$(sed -n 's/^ARG PYTHON_IMAGE=//p' Dockerfile)
[ -n "$BASE" ] || { echo "lock.sh: no 'ARG PYTHON_IMAGE=' line in Dockerfile" >&2; exit 1; }

uv_args=()
for arg in "$@"; do
  case "$arg" in
    --base)
      echo "Dockerfile base: $BASE"
      echo "latest $BASE_TAG: $(docker buildx imagetools inspect "$BASE_TAG" --format '{{json .Manifest.Digest}}')"
      echo "Update ARG PYTHON_IMAGE in the Dockerfile to <tag>@<digest> to pick up base-image security fixes."
      exit 0
      ;;
    *) uv_args+=("$arg") ;;
  esac
done

docker run --rm -v "$PWD":/src -w /src -e UV_CACHE_DIR=/tmp/uv -e PIP_ROOT_USER_ACTION=ignore "$BASE" \
  sh -euc '
    pip install --quiet --disable-pip-version-check "uv==$0"
    common="--universal --python-version 3.12 --generate-hashes --custom-compile-command scripts/lock.sh --quiet"
    uv pip compile requirements.txt $common "$@" -o requirements.lock
    # The dev lock must agree with the runtime lock on every shared package.
    grep -E "^[A-Za-z0-9]" requirements.lock | sed "s/ *\\\\\$//" > /tmp/pins-from-requirements.lock
    uv pip compile requirements-dev.txt $common "$@" -c /tmp/pins-from-requirements.lock -o requirements-dev.lock
  ' "$UV_VERSION" ${uv_args[@]+"${uv_args[@]}"}
echo "wrote requirements.lock and requirements-dev.lock"
