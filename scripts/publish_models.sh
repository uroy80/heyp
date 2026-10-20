#!/usr/bin/env bash
# Publish heyp's pinned ONNX models as the assets of the GitHub release "models-v1" (deploy/DEPLOY.md §7.1).
#
#   scripts/publish_models.sh DIR [--repo OWNER/NAME] [--target REF] [--required-only] [--dry-run]
#
# Every image build downloads the models from that release (scripts/fetch_models.py), so it has to exist before
# the workflows first run. DIR must hold every file named in MODELS of scripts/fetch_models.py with exactly the
# pinned size and sha256: that is checked first, and nothing is uploaded otherwise. To take them from an image
# you built:
#   docker create --name heyp-models heyp:dev && docker cp heyp-models:/opt/heyp/models ./models && docker rm heyp-models
# scripts/export_models/ reproduces heyp's own exports byte for byte (including the optional int8 BiRefNet).
#
# With release immutability on, assets cannot be added once the release is published, so the optional models are
# required too unless you pass --required-only. gh uploads the assets while the release is a draft and publishes
# it last. --target is the commit or branch the models-v1 tag is created on (default: the default branch, which
# must have at least one commit). --dry-run checks the files and prints the gh command without running it.
set -euo pipefail

REPO="uroy80/heyp" # keep in sync with RELEASE in scripts/fetch_models.py (tests/test_publish_models.py)
TAG="models-v1"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python="${PYTHON:-python3}"

usage() {
  echo "usage: scripts/publish_models.sh DIR [--repo OWNER/NAME] [--target REF] [--required-only] [--dry-run]" >&2
  exit 2
}

dir="" target="" optional="--with-optional" dry_run=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) [[ $# -ge 2 ]] || usage; REPO="$2"; shift 2 ;;
    --target) [[ $# -ge 2 ]] || usage; target="$2"; shift 2 ;;
    --required-only) optional=""; shift ;;
    --dry-run) dry_run=true; shift ;;
    -h|--help) usage ;;
    -*) echo "unknown option: $1" >&2; usage ;;
    *) [[ -z "$dir" ]] || usage; dir="$1"; shift ;;
  esac
done
[[ -n "$dir" ]] || usage
[[ -d "$dir" ]] || { echo "not a directory: $dir" >&2; exit 2; }

# 1. Every file is the pinned one (size + sha256), or stop here.
# shellcheck disable=SC2086 # $optional is empty or one flag
"$python" "$here/fetch_models.py" "$dir" --check $optional

files=() # (no mapfile: macOS still ships bash 3.2)
while IFS= read -r name; do
  [[ -z "$name" ]] || files+=("$dir/$name")
done < <(
  # shellcheck disable=SC2086
  "$python" "$here/fetch_models.py" --list $optional
)
[[ ${#files[@]} -gt 0 ]] || { echo "fetch_models.py --list returned nothing" >&2; exit 1; }

notes="ONNX models for heyp. Each file is pinned (size + sha256) in scripts/fetch_models.py, and image builds \
refuse anything else; scripts/export_models/ reproduces the models heyp exports itself byte for byte."
cmd=(gh release create "$TAG" --repo "$REPO" --title "Models v1" --notes "$notes")
[[ -z "$target" ]] || cmd+=(--target "$target")
cmd+=("${files[@]}")

if $dry_run; then
  printf '%q ' "${cmd[@]}"
  echo
  exit 0
fi

# 2. Never touch an existing release: its assets are what every past build verified.
command -v gh >/dev/null || { echo "gh (GitHub CLI) is required: https://cli.github.com" >&2; exit 1; }
if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  echo "release $TAG already exists in $REPO; check it with: python3 scripts/fetch_models.py --probe" >&2
  exit 1
fi

# 3. Create it: gh uploads to a draft, then publishes. CI's "Model sources" job (fetch_models.py --probe) then
#    confirms every required model can be downloaded.
"${cmd[@]}"
echo "published $TAG in $REPO with ${#files[@]} models"
