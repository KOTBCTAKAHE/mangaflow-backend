"""Export intfloat/multilingual-e5-small (MIT) to ONNX + dynamic int8, verify against torch.

Run in CI (needs torch/transformers); the server only needs onnxruntime + tokenizers.
"""
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort
import torch
from onnxruntime.quantization import QuantType, quantize_dynamic
from tokenizers import Tokenizer
from transformers import AutoModel, AutoTokenizer

NAME = "intfloat/multilingual-e5-small"
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
tok = AutoTokenizer.from_pretrained(NAME)
model = AutoModel.from_pretrained(NAME).eval()
tok.backend_tokenizer.save(str(out / "tokenizer.json"))

texts = [
    "query: герой переродился пауком в подземелье",
    "passage: Обычная старшеклассница погибает и перерождается в теле паука в подземелье, полном монстров.",
    "passage: Юноша вступает в баскетбольную команду школы и мечтает о национальном турнире.",
    "query: a girl reborn as a spider in a dungeon",
]
enc = tok(texts, padding=True, truncation=True, max_length=256, return_tensors="pt")
with torch.no_grad():
    ref = model(**enc).last_hidden_state
fp32 = out / "e5_small.onnx"
torch.onnx.export(model, (enc["input_ids"], enc["attention_mask"]), str(fp32),
                  input_names=["input_ids", "attention_mask"], output_names=["last_hidden_state"],
                  dynamic_axes={"input_ids": {0: "b", 1: "s"}, "attention_mask": {0: "b", 1: "s"},
                                "last_hidden_state": {0: "b", 1: "s"}}, opset_version=17)
q8 = out / "e5_small_int8.onnx"
quantize_dynamic(str(fp32), str(q8), weight_type=QuantType.QInt8)
fp32.unlink()


def pool(h, mask):
    m = mask[..., None].astype(np.float32)
    v = (h * m).sum(1) / np.maximum(m.sum(1), 1e-9)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


t = Tokenizer.from_file(str(out / "tokenizer.json"))
t.enable_truncation(256)
t.enable_padding(pad_id=t.token_to_id("<pad>"), pad_token="<pad>")
batch = t.encode_batch(texts)
ids = np.array([b.ids for b in batch], dtype=np.int64)
mask = np.array([b.attention_mask for b in batch], dtype=np.int64)
assert (ids == enc["input_ids"].numpy()).all(), "tokenizers.json differs from the HF tokenizer"
sess = ort.InferenceSession(str(q8), providers=["CPUExecutionProvider"])
got = pool(sess.run(None, {"input_ids": ids, "attention_mask": mask})[0], mask)
want = pool(ref.numpy(), enc["attention_mask"].numpy())
agree = (got * want).sum(1)
print("int8 vs fp32 cosine:", agree.round(4))
sims = got @ got.T
print("query→spider passage", round(float(sims[0, 1]), 3), "query→basketball", round(float(sims[0, 2]), 3),
      "en query→spider", round(float(sims[3, 1]), 3))
assert agree.min() > 0.97 and sims[0, 1] > sims[0, 2] and sims[3, 1] > sims[3, 2]
print("size MB", round(q8.stat().st_size / 1e6, 1))
