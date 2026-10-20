# Model exports

heyp ships three ONNX models that we convert ourselves from upstream PyTorch weights, plus one optional
variant. The Real-ESRGAN exports are reproducible byte for byte (checked on linux/arm64 and linux/amd64), so
anyone can rebuild them and compare hashes. BiRefNet's ONNX bytes depend on the CPU the export runs on (graph
constant folding), so it is verified by parity with PyTorch instead (`export_birefnet.py` fails above 1e-4;
the published copy is within 3e-9); the pinned copy in the `models-v1` release was built by the **Models**
workflow on a GitHub-hosted runner (`.github/workflows/models.yml`, run log public). Third-party ONNX files (YuNet, U²-Net human segmentation, the YOLOv9 plate detector) are used as
published and are not exported here.

| File | From | Licence | sha256 |
|---|---|---|---|
| `realesr-general-x4v3.onnx` (Upscale, "strong") | Real-ESRGAN `realesr-general-x4v3.pth`, release v0.2.5.0 | BSD-3-Clause | `afca20f0…04c7` |
| `realesr-general-x4v3-dn05.onnx` (Upscale, "balanced") | same, weights blended 0.5/0.5 with `realesr-general-wdn-x4v3.pth` | BSD-3-Clause | `3d1b12ec…6fcd` |
| `birefnet-lite-dc-1024.onnx` (Remove background, any subject) | `ZhengPeng7/BiRefNet_lite@aa62cd87` `model.safetensors` + model code | MIT | `15daa3c7…93f9` |
| `birefnet-lite-dc-1024.int8.onnx` (optional) | the file above, dynamic QUInt8 (MatMul + Conv) | MIT | `e0b9c02b…bef2` |

Full hashes: `SHA256SUMS` (they must equal the pins in `scripts/fetch_models.py`).

## Rebuild everything

```sh
scripts/export_models/export_all.sh /tmp/heyp-models   # Docker, ~8 GB RAM, ~10 min
```

It builds the exporter image (`Dockerfile`), downloads the pinned sources (`fetch_sources.py`: every file is
size- and sha256-checked, including the BiRefNet model code), runs the exports with networking off, and
finishes with `sha256sum -c SHA256SUMS`. To publish the `models-v1` release (the four `.onnx` files plus copies of
the three third-party models, which `fetch_models.py` uses as a fallback source), add the third-party files with
`python3 scripts/fetch_models.py OUT_DIR --with-optional` and run `scripts/publish_models.sh OUT_DIR`
(deploy/DEPLOY.md §7.1).

## Why these steps

- **torch from the CPU index.** On linux/aarch64 the PyPI `torchvision==0.29.1` wheel is a CUDA 13 build that
  drags in gigabytes of CUDA libraries. The Dockerfile installs `torch==2.14.1+cpu` and
  `torchvision==0.29.1+cpu` from `https://download.pytorch.org/whl/cpu`, then the rest from PyPI. Keep every
  version pinned: a different torch or onnx release may export a functionally equal but byte-different file.
- **Real-ESRGAN** (`export_sr.py`): spandrel loads the `.pth`; `torch.onnx.export` (TorchScript exporter,
  `dynamo=False`, opset 17) with dynamic batch/height/width. "balanced" is Real-ESRGAN's own default denoise
  strength: the two checkpoints averaged weight by weight (`W = 0.5·x4v3 + 0.5·wdn-x4v3`). The script checks
  onnxruntime against PyTorch (max difference ~4e-6).
- **BiRefNet_lite** (`export_birefnet.py`): the published ONNX files emulate torchvision's `deform_conv2d` with
  gathers, which needs ~6.4 GB at 1024² and drifts from PyTorch (1.8 % of pixels off by > 0.03). We register a
  symbolic that maps it to the standard ONNX `DeformConv` op (opset 19; onnxruntime ≥ 1.25 has a CPU kernel):
  ~1.6 GB peak with the arena and memory pattern off, max difference to PyTorch 1e-4. The graph outputs the
  logits of the final prediction; heyp applies a sigmoid.
- **int8** (`quantize_birefnet.py`): `onnxruntime.quantization.quant_pre_process` (with
  `skip_symbolic_shape=True`: symbolic shape inference has no rule for `DeformConv`), then `quantize_dynamic`
  (QUInt8, MatMul + Conv). Half the download (84 vs 181 MB) and ~140 MB less resident memory when idle, but
  the same speed (3.4 vs 3.3 s at 4 threads, 5.9 vs 5.8 s at 2) and the same peak (1.60 vs 1.64 GB), with
  slightly softer thin structures (soft-IoU vs fp32 0.970 on a rocket, 0.979 on a bicycle, ≥ 0.991 on
  people, pets and products) — so it is opt-in (`HEYP_MATTING_MODEL=int8`, `fetch_models.py --with-optional`).

## Verified

`export_sr.py` (both variants) and `quantize_birefnet.py` were re-run from these scripts against the pinned
sources and reproduced the pinned sha256s byte for byte (torch 2.14.1+cpu, spandrel 0.4.2, onnxruntime
1.31.0). `export_birefnet.py` (opset 19, `dynamo=False`, the weights load with no missing or unexpected keys)
produced the pinned `15daa3c7…` on a GitHub ubuntu-24.04 runner with max |sigmoid(torch) − sigmoid(onnxruntime)|
= 2.7e-9; on other CPUs the bytes differ but parity holds. `quantize_birefnet.py` turned that file into the pinned
int8 `e0b9c02b…`. A full re-export needs ~8 GB of RAM.

## Running a script by hand

```sh
docker build -t heyp-export-models scripts/export_models
docker run --rm -v "$PWD/scripts/export_models:/x:ro" -v /tmp/m:/work heyp-export-models \
  python -I /x/fetch_sources.py /work/src
docker run --rm --network none -v "$PWD/scripts/export_models:/x:ro" -v /tmp/m:/work heyp-export-models \
  python -I /x/export_sr.py /work/realesr-general-x4v3.onnx /work/src/realesr-general-x4v3.pth
```
