"""Real-ESRGAN .pth -> ONNX (opset 17, dynamic N/H/W) via spandrel; optional Real-ESRGAN "dni" weight blend.

usage: python -I export_sr.py OUT.onnx A.pth [B.pth WEIGHT_A]
       WEIGHT_A blends the two checkpoints: W = WEIGHT_A * A + (1 - WEIGHT_A) * B
       (heyp "balanced" = x4v3 0.5 + wdn-x4v3 0.5, Real-ESRGAN's own default denoise strength).

Deterministic: the same torch/spandrel versions give byte-identical files on arm64 and amd64.
"""
import hashlib
import sys

import numpy as np
import onnxruntime as ort
import spandrel
import torch


def main() -> None:
    torch.manual_seed(0)
    out, a = sys.argv[1], sys.argv[2]
    desc = spandrel.ModelLoader().load_from_file(a)
    if len(sys.argv) > 3:
        b, wa = sys.argv[3], float(sys.argv[4])
        sa = desc.model.state_dict()
        sb = spandrel.ModelLoader().load_from_file(b).model.state_dict()
        desc.model.load_state_dict({k: wa * sa[k] + (1 - wa) * sb[k] for k in sa})
    model = desc.model.eval().float()
    print("arch", desc.architecture.name, "scale", desc.scale,
          "params", sum(p.numel() for p in model.parameters()))
    x = torch.rand(1, 3, 64, 64)
    with torch.no_grad():
        torch.onnx.export(model, (x,), out, input_names=["input"], output_names=["output"], opset_version=17,
                          do_constant_folding=True, dynamo=False,
                          dynamic_axes={"input": {0: "n", 2: "h", 3: "w"}, "output": {0: "n", 2: "H", 3: "W"}})
        ref = model(x).numpy()
    sess = ort.InferenceSession(out, providers=["CPUExecutionProvider"])
    y = sess.run(None, {"input": x.numpy()})[0]
    diff = float(np.abs(y - ref).max())
    print("torch vs onnxruntime max abs diff", diff)
    if diff > 1e-3:
        raise SystemExit("export does not match PyTorch")
    print("sha256", hashlib.sha256(open(out, "rb").read()).hexdigest(), out)


if __name__ == "__main__":
    main()
