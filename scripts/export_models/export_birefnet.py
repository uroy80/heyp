"""BiRefNet_lite (MIT) -> ONNX with a native ONNX DeformConv node (onnxruntime >= 1.25 has a CPU kernel).

usage: python -I export_birefnet.py SOURCES_DIR WEIGHTS.safetensors OUT.onnx SIZE [OPSET]
       SOURCES_DIR contains birefnet_pkg/ (from fetch_sources.py); heyp ships SIZE 1024, OPSET 19.

The published BiRefNet ONNX files emulate torchvision's deform_conv2d with gathers: ~6.4 GB peak at
1024² and 1.8 % of pixels off. Mapping it to ``DeformConv`` gives ~1.6 GB and matches PyTorch to 1e-4.
The graph returns the logits of the final prediction (1x1xSIZExSIZE); apply a sigmoid.
"""
import hashlib
import sys
import time

import numpy as np
import onnxruntime as ort
import torch
from safetensors.torch import load_file
from torch.onnx import register_custom_op_symbolic
from torch.onnx import symbolic_helper as sh


def deform_conv2d(g, inp, weight, offset, mask, bias, stride_h, stride_w, pad_h, pad_w, dil_h, dil_w,
                  n_weight_grps, n_offset_grps, use_mask):
    def c(v):
        return sh._get_const(v, "i", "")

    ph, pw = c(pad_h), c(pad_w)
    return g.op("DeformConv", inp, weight, offset, bias, mask,
                dilations_i=[c(dil_h), c(dil_w)], group_i=c(n_weight_grps), offset_group_i=c(n_offset_grps),
                pads_i=[ph, pw, ph, pw], strides_i=[c(stride_h), c(stride_w)])


class Final(torch.nn.Module):
    def __init__(self, m):
        super().__init__()
        self.m = m  # the attribute name prefixes the ONNX initializer names: keep "m" or the sha256 changes

    def forward(self, x):
        return self.m(x)[-1]  # logits of the final prediction


def main() -> None:
    src, weights, out, size = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
    opset = int(sys.argv[5]) if len(sys.argv) > 5 else 19
    sys.path.insert(0, src)
    from birefnet_pkg.birefnet import BiRefNet
    from birefnet_pkg.BiRefNet_config import BiRefNetConfig

    register_custom_op_symbolic("torchvision::deform_conv2d", deform_conv2d, opset)
    torch.manual_seed(0)
    net = BiRefNet(config=BiRefNetConfig(bb_pretrained=False))
    missing, unexpected = net.load_state_dict(load_file(weights), strict=False)
    if missing or unexpected:
        raise SystemExit(f"weights do not fit the model: missing {missing[:3]}, unexpected {unexpected[:3]}")
    model = Final(net.eval()).eval()
    x = torch.rand(1, 3, size, size)
    t0 = time.time()
    with torch.no_grad():
        ref = model(x).numpy()
        print("torch forward", round(time.time() - t0, 1), "s", ref.shape)
        torch.onnx.export(model, (x,), out, input_names=["input_image"], output_names=["output_image"],
                          opset_version=opset, do_constant_folding=True, dynamo=False)
    opts = ort.SessionOptions()
    opts.enable_cpu_mem_arena = False
    opts.enable_mem_pattern = False
    y = ort.InferenceSession(out, sess_options=opts, providers=["CPUExecutionProvider"]).run(
        None, {"input_image": x.numpy()})[0]

    def sig(a):
        return 1 / (1 + np.exp(-a))

    diff = float(np.abs(sig(y) - sig(ref)).max())
    print("max |sigmoid(torch) - sigmoid(onnxruntime)|", diff)
    if diff > 1e-2:
        raise SystemExit("export does not match PyTorch")
    print("sha256", hashlib.sha256(open(out, "rb").read()).hexdigest(), out)


if __name__ == "__main__":
    main()
