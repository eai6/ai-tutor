"""Parity between the ONNX encoder and sentence-transformers.

The offline desktop build swaps ``kb_storage.embed`` from sentence-transformers
(~500 MB installed, needs torch) to onnxruntime (~90 MB, no torch). That is
only safe if the two produce the same vectors — a drifted encoder would put
the desktop app's queries in a different space from the corpus embedded on the
server, and retrieval would degrade *quietly*, returning plausible-looking but
wrong chunks.

Skipped unless the artifact has been built:
    venv/bin/python scripts/export_minilm_onnx.py

Plan: memory/desktop_offline_app_plan.md (Phase 0)
"""

import unittest

from django.test import TestCase, override_settings

from ai_tutor.apps.curriculum import kb_storage

PROBES = [
    'A map scale is the ratio between map distance and ground distance.',
    'Photosynthesis converts light energy into chemical energy in chloroplasts.',
    'x',                                    # single token
    'What is 1:25,000?',                    # punctuation + digits
    'Rivers, tributaries and confluence '   # long, crosses padding boundary
    'describe how a drainage basin collects water across its catchment area '
    'before discharging into the sea at the river mouth.',
]


def _onnx_available() -> bool:
    directory = kb_storage._onnx_dir()
    return (directory / 'model.onnx').exists() and (directory / 'tokenizer.json').exists()


@unittest.skipUnless(_onnx_available(),
                     'ONNX artifact not built (scripts/export_minilm_onnx.py)')
class OnnxEmbeddingParityTest(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import numpy as np
        cls.np = np
        with override_settings(EMBEDDING_BACKEND='local'):
            cls.reference = np.asarray(kb_storage.embed(PROBES), dtype=np.float32)
        with override_settings(EMBEDDING_BACKEND='onnx'):
            cls.produced = np.asarray(kb_storage.embed(PROBES), dtype=np.float32)

    def test_same_shape(self):
        self.assertEqual(self.produced.shape, (len(PROBES), 384))

    def test_output_is_l2_normalised(self):
        """sentence-transformers all-MiniLM-L6-v2 ends in a Normalize module.
        Vectors that aren't unit length would not compare against the corpus
        embedded on the server."""
        norms = self.np.linalg.norm(self.produced, axis=1)
        for n in norms:
            self.assertAlmostEqual(float(n), 1.0, places=5)

    def test_cosine_parity_per_probe(self):
        sims = (self.reference * self.produced).sum(axis=1) / (
            self.np.linalg.norm(self.reference, axis=1)
            * self.np.linalg.norm(self.produced, axis=1)
        )
        for probe, sim in zip(PROBES, sims):
            self.assertGreaterEqual(
                float(sim), 0.999,
                f'ONNX diverges from reference on {probe[:40]!r}: cosine {sim:.6f}',
            )

    def test_relative_ranking_is_preserved(self):
        """The property retrieval actually depends on: whichever probe the
        reference calls nearest, ONNX must call nearest too. Per-vector cosine
        can look fine while neighbour ordering still shifts."""
        query = self.produced[0]
        ref_order = self.np.argsort(-(self.reference @ self.reference[0]))
        onnx_order = self.np.argsort(-(self.produced @ query))
        self.assertEqual(list(ref_order), list(onnx_order))

    def test_batch_matches_single(self):
        """Padding must not change a short text's vector when it shares a batch
        with a long one — that is what the attention-mask pooling protects."""
        with override_settings(EMBEDDING_BACKEND='onnx'):
            alone = self.np.asarray(kb_storage.embed([PROBES[2]]), dtype=self.np.float32)[0]
        batched = self.produced[2]
        sim = float(alone @ batched / (self.np.linalg.norm(alone) * self.np.linalg.norm(batched)))
        self.assertGreaterEqual(sim, 0.999)


class OnnxBackendSelectionTest(TestCase):
    """The backend switch itself, independent of the artifact existing."""

    def test_default_backend_is_local(self):
        from django.conf import settings
        self.assertEqual(getattr(settings, 'EMBEDDING_BACKEND', 'local'), 'local')

    @override_settings(EMBEDDING_BACKEND='onnx', MINILM_ONNX_DIR='/nonexistent/path')
    def test_missing_artifact_fails_loudly(self):
        """A missing model must raise, not silently fall back to torch. A
        desktop build that quietly reverted to sentence-transformers would
        ship the 500 MB dependency it was meant to drop."""
        kb_storage._ONNX_SESSION = None
        kb_storage._ONNX_TOKENIZER = None
        with self.assertRaises(FileNotFoundError):
            kb_storage.embed(['anything'])
