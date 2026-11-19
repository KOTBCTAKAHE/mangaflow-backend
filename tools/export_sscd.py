"""Export Meta's SSCD copy-detection model (MIT) from TorchScript to ONNX and verify it.

Run in CI (needs torch); the server only needs onnxruntime.
"""
import sys

import numpy as np
import onnxruntime as ort
import torch

src, dst = sys.argv[1], sys.argv[2]
model = torch.jit.load(src, map_location="cpu").eval()
x = torch.randn(2, 3, 320, 320)
with torch.no_grad():
    ref = model(x).numpy()
    torch.onnx.export(model, x, dst, input_names=["image"], output_names=["embedding"],
                      dynamic_axes={"image": {0: "batch"}, "embedding": {0: "batch"}}, opset_version=17)
sess = ort.InferenceSession(dst, providers=["CPUExecutionProvider"])
out = sess.run(None, {"image": x.numpy()})[0]
print("shape", out.shape, "max abs diff", float(np.abs(out - ref).max()))
assert out.shape == (2, 512) and np.allclose(out, ref, atol=1e-3)
