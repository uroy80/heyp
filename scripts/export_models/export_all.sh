#!/bin/sh
# Rebuild every model heyp exports itself and check the results against SHA256SUMS (= the pins in
# scripts/fetch_models.py). Usage: scripts/export_models/export_all.sh [OUT_DIR]   (default ./models-export)
# Needs Docker and ~8 GB of RAM for the BiRefNet export. Sources are downloaded once (pinned) into OUT_DIR/src.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
out=$(mkdir -p "${1:-models-export}" && cd "${1:-models-export}" && pwd)
image=${HEYP_EXPORT_IMAGE:-heyp-export-models}
mkdir -p "$out/src"
docker build -t "$image" "$here"
run() { docker run --rm --user "$(id -u):$(id -g)" -v "$here:/x:ro" -v "$out:/work" "$@"; }
# 1. pinned sources (network)
run "$image" python -I /x/fetch_sources.py /work/src
# 2. exports (no network)
run --network none "$image" sh -c '
  set -e
  python -I /x/export_sr.py /work/realesr-general-x4v3.onnx /work/src/realesr-general-x4v3.pth
  python -I /x/export_sr.py /work/realesr-general-x4v3-dn05.onnx /work/src/realesr-general-x4v3.pth \
         /work/src/realesr-general-wdn-x4v3.pth 0.5
  python -I /x/export_birefnet.py /work/src /work/src/birefnet_lite.safetensors /work/birefnet-lite-dc-1024.onnx 1024 19
  python -I /x/quantize_birefnet.py /work/birefnet-lite-dc-1024.onnx /work/birefnet-lite-dc-1024.int8.onnx'
# 3. Real-ESRGAN exports must match the pins byte for byte. BiRefNet's ONNX bytes depend on the CPU the export ran
#    on (constant folding), so it is verified by parity with PyTorch instead (export_birefnet.py fails above 1e-4);
#    the copy in the models-v1 release, built by the Models workflow on a GitHub runner, is the pinned one.
cd "$out"
grep -F ' realesr-' "$here/SHA256SUMS" | sha256sum -c -
for f in birefnet-lite-dc-1024.onnx birefnet-lite-dc-1024.int8.onnx; do
  if grep -F " $f" "$here/SHA256SUMS" | sha256sum -c --status - 2>/dev/null; then
    echo "$f: OK (matches the pin)"
  else
    echo "$f: parity with PyTorch verified; bytes differ from the pin: sha256 $(sha256sum "$f" | cut -d' ' -f1), $(wc -c <"$f") bytes"
  fi
done
echo "exports verified; add the third-party models (python3 scripts/fetch_models.py $out --with-optional), then: scripts/publish_models.sh $out"
