#!/usr/bin/env python3
"""Export all-MiniLM-L6-v2 to ONNX for the offline desktop build.

    venv/bin/python scripts/export_minilm_onnx.py

Why this exists: ``kb_storage.embed`` runs sentence-transformers, which drags
in torch — roughly 500 MB installed to produce a 384-d vector from a 90 MB
encoder. The desktop bundle pays that on every install, on three platforms,
for one small model. onnxruntime + tokenizers are already dependencies and
need no torch at runtime.

Exports from the **locally cached sentence-transformers weights**, not from a
fresh download, so the ONNX graph provably corresponds to the encoder it
replaces. torch is needed to run this script; it is not needed to *use* the
output.

Outputs (gitignored — build artifacts, and one is ~90 MB):

    models/minilm-l6-v2/model.onnx
    models/minilm-l6-v2/tokenizer.json

Both go in the USB provisioning bundle. See memory/desktop_offline_app_plan.md.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / 'models' / 'minilm-l6-v2'

# Opset 14: the floor for scaled_dot_product_attention lowering in the BERT
# graph. Lower opsets export but emit a slower decomposed attention subgraph.
OPSET = 14


def main(check_parity: bool = True) -> int:
    import torch
    from sentence_transformers import SentenceTransformer

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # device='cpu' is required, not a preference: on this Mac the model
    # otherwise lands on MPS and the tracer dies with "Placeholder storage has
    # not been allocated on MPS device!". It also makes the parity reference
    # below CPU-computed, matching how onnxruntime will run it.
    st = SentenceTransformer('all-MiniLM-L6-v2', device='cpu')
    # st[0] is the Transformer module; .auto_model is the raw HF BertModel.
    # Exporting the bare encoder (not the SentenceTransformer wrapper) keeps
    # the graph to one input/one output — pooling and normalisation are
    # reimplemented in NumPy at inference time, where they are ~6 lines.
    encoder = st[0].auto_model
    encoder.eval()

    # The TorchScript exporter feeds the example tuple positionally, and
    # transformers 5.x BertModel.forward takes `use_cache` early enough in its
    # signature that the third positional lands on it
    # ("got multiple values for argument 'use_cache'"). This wrapper pins the
    # three inputs by keyword and returns the single tensor we pool.
    class Encoder(torch.nn.Module):
        def __init__(self, model):
            super().__init__()
            self.model = model

        def forward(self, input_ids, attention_mask, token_type_ids):
            return self.model(
                input_ids=input_ids,
                attention_mask=attention_mask,
                token_type_ids=token_type_ids,
            ).last_hidden_state

    encoder = Encoder(encoder).eval()

    tokenizer = st.tokenizer
    tokenizer.backend_tokenizer.save(str(OUT_DIR / 'tokenizer.json'))
    print(f"wrote {OUT_DIR / 'tokenizer.json'}")

    # Two sequences of different lengths, so the dynamic axes are exercised
    # during tracing rather than baked to a constant.
    sample = tokenizer(
        ['a short one', 'a noticeably longer sample sentence for tracing'],
        padding=True, truncation=True, max_length=256, return_tensors='pt',
    )
    args = (sample['input_ids'], sample['attention_mask'], sample['token_type_ids'])

    onnx_path = OUT_DIR / 'model.onnx'
    with torch.no_grad():
        torch.onnx.export(
            encoder,
            args,
            str(onnx_path),
            input_names=['input_ids', 'attention_mask', 'token_type_ids'],
            output_names=['last_hidden_state'],
            dynamic_axes={
                'input_ids': {0: 'batch', 1: 'seq'},
                'attention_mask': {0: 'batch', 1: 'seq'},
                'token_type_ids': {0: 'batch', 1: 'seq'},
                'last_hidden_state': {0: 'batch', 1: 'seq'},
            },
            opset_version=OPSET,
            do_constant_folding=True,
            # torch 2.10 defaults dynamo=True, which routes through
            # onnxscript — a dependency this project doesn't have and doesn't
            # need, since the runtime only consumes the .onnx file. The legacy
            # TorchScript exporter handles a plain BertModel trace fine.
            dynamo=False,
        )
    size_mb = onnx_path.stat().st_size / 1e6
    print(f"wrote {onnx_path} ({size_mb:.0f} MB)")

    # Fail the export rather than ship a graph that silently disagrees with
    # the encoder it replaces. The threshold matches the parity test in
    # apps/curriculum/tests/test_onnx_embedding_parity.py.
    #
    # Skippable because this comparison goes through kb_storage, which needs a
    # configured Django. That is free on a workstation and awkward inside a
    # Docker builder stage, which has the dependencies but not the app or its
    # settings. The same comparison runs in CI as
    # test_onnx_embedding_parity.py, against the artifact this produces.
    if not check_parity:
        print('parity check skipped (--skip-parity)')
        return 0

    from ai_tutor.apps.curriculum import kb_storage
    import numpy as np

    probes = [
        'A map scale is the ratio between map distance and ground distance.',
        'Photosynthesis converts light energy into chemical energy.',
        'x',
        'A much longer probe sentence intended to cross the padding boundary '
        'so that attention masking is exercised on a real batch.',
    ]
    reference = np.asarray(st.encode(probes, convert_to_numpy=True), dtype=np.float32)
    produced = np.asarray(kb_storage._embed_onnx(probes), dtype=np.float32)
    sims = (reference * produced).sum(axis=1) / (
        np.linalg.norm(reference, axis=1) * np.linalg.norm(produced, axis=1)
    )
    worst = float(sims.min())
    print(f"parity vs sentence-transformers: worst cosine {worst:.6f}")
    if worst < 0.999:
        print('FAILED: ONNX output diverges from the reference encoder',
              file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    check_parity = '--skip-parity' not in sys.argv
    if check_parity:
        # Only when the parity check will actually run — django.setup() needs
        # the app on the path and a settings module, neither of which exists
        # where this runs purely to produce the artifact.
        sys.path.insert(0, str(ROOT))
        import os
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ai_tutor.config.settings')
        import django
        django.setup()
    raise SystemExit(main(check_parity=check_parity))
