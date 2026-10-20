"""Optional int8 variant of the BiRefNet export: ORT pre-processing, then dynamic QUInt8 quantisation of
MatMul and Conv weights.

usage: python -I quantize_birefnet.py IN.onnx OUT.onnx

``quant_pre_process`` (ONNX shape inference + onnxruntime graph optimisation; symbolic shape inference is
skipped because it has no rule for DeformConv) folds the shape arithmetic first, so twice as many MatMuls get
int8 weights and the graph shrinks from ~16k to ~2.5k nodes. Produced the pinned sha256 (e0b9c02b…) from
the pinned fp32 export with onnxruntime 1.31.0.

84 MB instead of 181 MB; masks are near-identical (soft-IoU 0.970–0.9997 vs fp32 on 7 test photos) but
neither faster (3.4 vs 3.3 s at 4 threads) nor lighter at peak (1.60 vs 1.64 GB), so heyp ships fp32 by
default (``HEYP_MATTING_MODEL=int8`` opts in; ``fetch_models.py --with-optional`` downloads it).
"""
import hashlib
import os
import sys
import tempfile

from onnxruntime.quantization import QuantType, quantize_dynamic
from onnxruntime.quantization.shape_inference import quant_pre_process


def main() -> None:
    src, out = sys.argv[1], sys.argv[2]
    with tempfile.TemporaryDirectory(dir=os.path.dirname(os.path.abspath(out))) as tmp:
        pre = os.path.join(tmp, "pre.onnx")
        quant_pre_process(src, pre, skip_symbolic_shape=True)  # no symbolic rule for DeformConv
        quantize_dynamic(pre, out, weight_type=QuantType.QUInt8, op_types_to_quantize=["MatMul", "Conv"])
    print("sha256", hashlib.sha256(open(out, "rb").read()).hexdigest(), out)


if __name__ == "__main__":
    main()
